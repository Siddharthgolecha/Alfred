"""Phase 3: durable local reminders and read-only condition watches.

SQLite schedules/outbox work without the microphone or LLMs. Recurring rules
are deterministic, bounded, and are *not* arbitrary program execution.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json
import logging
import re
import time

LOG = logging.getLogger(__name__)

def _zone(name):
    if not isinstance(name, str) or len(name)>90 or not re.fullmatch(r"[A-Za-z0-9_+\-/]+",name):
        raise ValueError("Timezone must be an IANA name, e.g. Asia/Kolkata")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {name}; install tzdata if needed") from exc

def _time(hhmm):
    if not isinstance(hhmm,str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",hhmm):
        raise ValueError("local_time must be HH:MM in 24-hour time")
    hh,mm=(int(x) for x in hhmm.split(":"))
    return hh,mm

def parse_aware_time(value,now=None):
    """Require explicit offset or Z, reject local timestamps missing zone."""
    if not isinstance(value,str) or len(value)>45:
        raise ValueError("when must be an RFC3339/ISO timestamp with explicit offset")
    try:
        dt=datetime.fromisoformat(value.replace("Z","+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid timestamp; use 2026-10-14T20:00:00+05:30") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("Timestamp must include UTC offset or Z")
    timestamp=int(dt.timestamp())
    now=int(time.time()) if now is None else int(now)
    if timestamp<=now:
        raise ValueError("Reminder time must be in the future")
    if timestamp>now+5*366*86400:
        raise ValueError("Reminder cannot be scheduled more than five years ahead")
    return timestamp

def next_daily_time(local_time, timezone_name,now=None):
    """Find next valid occurrence; nonexistent DST wall-times skip that date.

    Ambiguous fall-back times use fold=0 (first occurrence). Missing spring-
    forward times are skipped, not silently moved to another clock time.
    """
    hh,mm=_time(local_time)
    zone=_zone(timezone_name)
    now=int(time.time()) if now is None else int(now)
    today=datetime.fromtimestamp(now,zone).date()
    for offset in range(370):
        day=today+timedelta(days=offset)
        naive=datetime(day.year,day.month,day.day,hh,mm)
        candidate=naive.replace(tzinfo=zone,fold=0)
        timestamp=int(candidate.timestamp())
        # Roundtrip catches imaginary spring-forward local times.
        if timestamp<=now or datetime.fromtimestamp(timestamp,zone).replace(tzinfo=None)!=naive:
            continue
        return timestamp
    raise ValueError("No valid daily occurrence found within one year")

def _message(value):
    if not isinstance(value,str) or not value.strip() or len(value)>240:
        raise ValueError("Reminder message must contain 1 to 240 characters")
    return value.strip()

class Automations:
    """SQLite-backed job registry and notification outbox.

    A completed notification is persisted on callback success; after crashes
    between delivery and ACK, at-least-once delivery may produce duplicates.
    """
    def __init__(self,store):
        self.store=store
        with store.lock,store.db:
            store.db.execute("""
                CREATE TABLE IF NOT EXISTS automation_jobs(
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','paused','completed','cancelled')),
                    payload TEXT NOT NULL, next_at INTEGER NOT NULL,
                    interval_minutes INTEGER, timezone TEXT, local_time TEXT,
                    armed INTEGER NOT NULL DEFAULT 1,
                    last_value REAL, last_triggered_at INTEGER,
                    last_error TEXT, created_at INTEGER NOT NULL)
            """)
            store.db.execute("""
                CREATE TABLE IF NOT EXISTS automation_outbox(
                    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL,
                    due_at INTEGER NOT NULL, message TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','sending','delivered')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at INTEGER NOT NULL,
                    lease_until INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(job_id,due_at))
            """)
            store.db.execute("CREATE INDEX IF NOT EXISTS idx_auto_jobs ON automation_jobs(status,next_at)")
            store.db.execute("CREATE INDEX IF NOT EXISTS idx_auto_outbox ON automation_outbox(status,retry_at)")

    def _create(self,kind,payload,next_at,interval=None,tz=None,local_time=None):
        with self.store.lock,self.store.db:
            cur=self.store.db.execute(
                "INSERT INTO automation_jobs(kind,status,payload,next_at,interval_minutes,timezone,local_time,created_at)"
                " VALUES(?,'active',?,?,?,?,?,?)",
                (kind,json.dumps(payload,sort_keys=True),next_at,interval,tz,local_time,int(time.time())))
            return {"id":cur.lastrowid,"kind":kind,"status":"active",
                    "next_at":next_at,"timezone":tz}

    def remind_at(self,message,when):
        return self._create("reminder_once",{"message":_message(message)},parse_aware_time(when))

    def remind_daily(self,message,local_time,timezone_name):
        return self._create("reminder_daily",{"message":_message(message)},
                            next_daily_time(local_time,timezone_name),
                            tz=timezone_name,local_time=local_time)

    def watch_gpu(self,threshold_c,interval_minutes,cooldown_minutes):
        if type(threshold_c) is not int or not 20<=threshold_c<=110:
            raise ValueError("GPU threshold must be 20..110 C")
        if type(interval_minutes) is not int or not 1<=interval_minutes<=1440:
            raise ValueError("Watch interval must be 1..1440 minutes")
        if type(cooldown_minutes) is not int or not 1<=cooldown_minutes<=10080:
            raise ValueError("Watch cooldown must be 1..10080 minutes")
        payload={"threshold_c":threshold_c,"cooldown_minutes":cooldown_minutes,
                 "hysteresis_c":3}
        return self._create("gpu_temperature_watch",payload,
                            int(time.time())+interval_minutes*60,
                            interval=interval_minutes)

    def list_jobs(self,all_states=False):
        sql=("SELECT id,kind,status,payload,next_at,interval_minutes,timezone,local_time,"
             "armed,last_value,last_triggered_at,last_error FROM automation_jobs")
        if not all_states:
            sql+=" WHERE status IN ('active','paused')"
        sql+=" ORDER BY id DESC LIMIT 200"
        with self.store.lock:
            rows=self.store.db.execute(sql).fetchall()
        return [{**dict(r),"payload":json.loads(r["payload"])} for r in rows]

    def change(self,job_id,action):
        if action not in ("pause","resume","cancel") or type(job_id) is not int or job_id<1:
            raise ValueError("Invalid job id or action")
        states={"pause":("active","paused"),
                "resume":("paused","active"),
                "cancel":("active","cancelled")}
        with self.store.lock,self.store.db:
            row=self.store.db.execute("SELECT * FROM automation_jobs WHERE id=?",(job_id,)).fetchone()
            if row is None:
                return {"changed":False,"reason":"unknown job"}
            previous=states[action][0]
            destination=states[action][1]
            # A paused job is also allowed to be cancelled.
            if action=="cancel" and row["status"]=="paused":
                previous="paused"
            if row["status"]!=previous:
                return {"changed":False,"reason":f"job is {row['status']}"}
            next_at=row["next_at"]
            if action=="resume":
                if row["kind"]=="reminder_daily":
                    next_at=next_daily_time(row["local_time"],row["timezone"])
                elif row["kind"]=="gpu_temperature_watch":
                    next_at=int(time.time())+row["interval_minutes"]*60
                # A paused overdue one-off will still be delivered.
            cur=self.store.db.execute(
                "UPDATE automation_jobs SET status=?,next_at=? WHERE id=? AND status=?",
                (destination,next_at,job_id,previous))
            return {"changed":cur.rowcount==1,"id":job_id,"status":destination}

    def _enqueue(self,job_id,due_at,message,now):
        self.store.db.execute(
            "INSERT OR IGNORE INTO automation_outbox(job_id,due_at,message,status,retry_at)"
            " VALUES(?,?,?,'pending',?)",(job_id,due_at,message,now))

    def _claim_jobs(self,now):
        """Atomic due-date advancement before expensive read-only measurements."""
        jobs=[]
        with self.store.lock,self.store.db:
            rows=self.store.db.execute(
                "SELECT * FROM automation_jobs WHERE status='active' AND next_at<=?"
                " ORDER BY next_at LIMIT 40",(now,)).fetchall()
            for r in rows:
                payload=json.loads(r["payload"])
                if r["kind"]=="reminder_once":
                    cur=self.store.db.execute(
                        "UPDATE automation_jobs SET status='completed' WHERE id=? AND status='active'"
                        " AND next_at=?",(r["id"],r["next_at"]))
                    if cur.rowcount:
                        self._enqueue(r["id"],r["next_at"],payload["message"],now)
                elif r["kind"]=="reminder_daily":
                    next_at=next_daily_time(r["local_time"],r["timezone"],now)
                    cur=self.store.db.execute(
                        "UPDATE automation_jobs SET next_at=? WHERE id=? AND status='active'"
                        " AND next_at=?",(next_at,r["id"],r["next_at"]))
                    if cur.rowcount:
                        # Coalesce missed recurrences to a single reminder per restart.
                        self._enqueue(r["id"],r["next_at"],payload["message"],now)
                elif r["kind"]=="gpu_temperature_watch":
                    new_due=now+r["interval_minutes"]*60
                    cur=self.store.db.execute(
                        "UPDATE automation_jobs SET next_at=? WHERE id=? AND status='active'"
                        " AND next_at=?",(new_due,r["id"],r["next_at"]))
                    if cur.rowcount:
                        jobs.append((dict(r),payload))
        return jobs

    def _record_watch(self,job,payload,reading,now):
        temp=reading.get("gpu_temp_c") if isinstance(reading,dict) else None
        if type(temp) not in (int,float):
            with self.store.lock,self.store.db:
                self.store.db.execute(
                    "UPDATE automation_jobs SET last_error=? WHERE id=?",
                    ("GPU temperature unavailable",job["id"]))
            self.store.event("watch_unavailable",{"job_id":job["id"]})
            return
        threshold=payload["threshold_c"]
        hyst=payload["hysteresis_c"]
        with self.store.lock,self.store.db:
            row=self.store.db.execute(
                "SELECT status,armed,last_triggered_at FROM automation_jobs WHERE id=?",
                (job["id"],)).fetchone()
            if not row or row["status"]!="active":
                return
            armed=bool(row["armed"])
            last=row["last_triggered_at"] or 0
            if not armed and temp<=threshold-hyst:
                armed=True
            if armed and temp>=threshold and now-last>=payload["cooldown_minutes"]*60:
                self._enqueue(job["id"],now,
                    f"GPU temperature alert: {temp:g} C is at or above {threshold} C",now)
                armed=False
                last=now
            self.store.db.execute(
                "UPDATE automation_jobs SET last_value=?,armed=?,last_triggered_at=?,"
                "last_error=NULL WHERE id=? AND status='active'",
                (temp,int(armed),last,job["id"]))

    def _claim_notification(self,now):
        with self.store.lock,self.store.db:
            row=self.store.db.execute(
                "SELECT * FROM automation_outbox WHERE "
                "(status='pending' AND retry_at<=?) OR (status='sending' AND lease_until<=?)"
                " ORDER BY due_at,id LIMIT 1",(now,now)).fetchone()
            if row is None:
                return None
            cur=self.store.db.execute(
                "UPDATE automation_outbox SET status='sending',lease_until=?,attempts=attempts+1"
                " WHERE id=? AND ((status='pending' AND retry_at<=?)"
                " OR (status='sending' AND lease_until<=?))",
                (now+60,row["id"],now,now))
            return dict(row) if cur.rowcount else None

    def _finish_notification(self,row,now,error=None):
        with self.store.lock,self.store.db:
            if error:
                delay=min(3600,30*2**min(6,int(row["attempts"])))
                self.store.db.execute(
                    "UPDATE automation_outbox SET status='pending',retry_at=?,lease_until=0"
                    " WHERE id=? AND status='sending'",(now+delay,row["id"]))
            else:
                self.store.db.execute(
                    "UPDATE automation_outbox SET status='delivered',lease_until=0"
                    " WHERE id=? AND status='sending'",(row["id"],))

    def tick(self,tools,on_reminder,now=None):
        """One scheduler iteration; safe to call after restart.

        Watch checks do not invoke Qwen or mutating system tools.
        Notifications retry on errors, with at-least-once semantics.
        """
        now=int(time.time()) if now is None else int(now)
        watches=self._claim_jobs(now)
        for job,payload in watches:
            try:
                metrics=tools.execute("get_gpu_status",{},from_plan=True)
                self._record_watch(job,payload,metrics,now)
            except Exception as exc:
                LOG.warning("Watch %s failed: %s",job["id"],exc)
                self.store.event("watch_error",{"job_id":job["id"],"error":str(exc)})
        # Bounded batch; never starve other system-agent work.
        for _ in range(20):
            item=self._claim_notification(now)
            if item is None:
                break
            try:
                on_reminder({"id":item["job_id"],"message":item["message"],
                             "due_at":item["due_at"],"delivery_id":item["id"]})
            except Exception as exc:
                self._finish_notification(item,now,error=exc)
                self.store.event("delivery_error",{"id":item["id"],"error":str(exc)})
            else:
                self._finish_notification(item,now)
                self.store.event("delivery_sent",{"id":item["id"],"job_id":item["job_id"]})
