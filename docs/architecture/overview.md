# Architecture overview

Alfred is a single Python process that turns microphone audio into validated tool calls. Two
local models take part, but neither one executes anything. Python code validates and runs every
action.

## Design rules

1. **Models propose, Python disposes.** Needle 3 and Qwen return structured *proposals*. The
   tool registry in `runtime.py` and the policy in `system2.py` decide what runs.
2. **Wake-gated input.** Transcribed speech that is not addressed to the wake phrase is dropped
   before it reaches a model or tool.
3. **Local approval for anything sensitive.** Approvals are a one-time CLI action
   (`alfred --approve ID`). A spoken "yes" cannot approve.
4. **Fail closed.** A missing microphone, an unaudited Harness runtime, a malformed plan or an
   occupied model port stops the operation. Nothing falls back to a weaker path.
5. **Local only.** The inference endpoint must be a loopback HTTP origin
   (`127.0.0.1`, `localhost` or `::1`). See [Security model](security-model.md).

## System context

```{mermaid}
flowchart LR
    user([User])
    mic[/"Microphone<br/>Sony WI-C100 or USB/analog"/]
    subgraph jetson["Jetson Orin Nano 8GB"]
        direction TB
        alfred["Alfred process<br/>(alfred.app / alfred.cli)"]
        sched["Scheduler threads<br/>reminders, watches, workflows"]
        db[("SQLite WAL<br/>alfred.db")]
        backend["Local inference server<br/>llama.cpp / llama-server /<br/>Ollama / LM Studio"]
        cordis["Cordis SDK child process<br/>(DeepSeek Harness, no tools)"]
        audio["PipeWire / PulseAudio<br/>+ BlueZ"]
        sys["systemctl --user<br/>named task scripts<br/>tegrastats"]
    end
    cli(["Local CLI<br/>--approve, --pending, close"])

    user -- speech --> mic --> audio -- "parec PCM 16 kHz" --> alfred
    alfred <--> db
    alfred -- "OpenAI-style HTTP<br/>(loopback)" --> backend
    alfred -- "JSON-RPC stdio" --> cordis
    cordis -- "HTTP (loopback)" --> backend
    alfred -- "allowlisted only" --> sys
    sched <--> db
    sched --> sys
    cli -- "approve / reject / close" --> alfred
    cli <--> db
    alfred -- "spd-say (optional)" --> user
```

## Components

[`Alfred`](../../alfred/app.py) is the composition root. Its constructor builds services in four
groups:

| Group | Services | Source |
|---|---|---|
| Routing | `Store`, `SessionManager`, `InferenceAdmission` | `runtime.py`, `session.py`, `inference_admission.py` |
| Audio | `ChildProcesses`, `Feedback`, `AudioManager`, `WakeWordGate` | `lifecycle.py`, `feedback.py`, `audio.py`, `wakeword.py` |
| Models | `Tools`, `ModelSupervisor` (alias `LlamaSupervisor`), `Qwen`, `System2`, `LocalHarness`, `Cactus` | `runtime.py`, `backends.py`, `models.py`, `system2.py`, `cognitive/harness_sdk.py` |
| Lifecycle | `SkillRegistry`, `Scheduler`, `RequestRouter`, `WakeWordListener` | `skills.py`, `runtime.py`, `router.py`, `listener.py` |

`Cactus` is only created when models are loaded. Administrative CLI commands such as
`--approve`, `--pending` and `--workflow-*` build `Alfred(config, load_models=False)`, so they
never load Needle or Whistle.

```{mermaid}
flowchart TB
    subgraph entry["Entry points"]
        main["cli.main / _main"]
    end
    subgraph io["Input and output"]
        audio["AudioManager<br/>parec capture, source probe"]
        utt["UtteranceStream<br/>RMS voice activity"]
        wake["WakeWordGate<br/>+ AcousticWakeDetector (optional)"]
        listener["WakeWordListener"]
        fb["Feedback<br/>console + spd-say"]
    end
    subgraph decide["Decision layer"]
        router["RequestRouter"]
        needle["Cactus<br/>Whistle STT + Needle 3"]
        loop["NeedleAgentLoop"]
        ctx["ContextManager<br/>SessionSlots"]
        esc["escalation.build_handoff"]
        harness["LocalHarness<br/>Qwen via Cordis"]
    end
    subgraph policy["Policy layer"]
        s2["System2<br/>preflight, stage, approve"]
        appr["Approvals"]
        val["runtime.validate / validate_plan"]
    end
    subgraph act["Execution layer"]
        tools["Tools"]
        wf["WorkflowEngine"]
        auto["Automations"]
        sched["Scheduler"]
    end
    subgraph state["State"]
        store[("Store (SQLite)")]
        ledger["SessionLedger"]
    end
    subgraph infra["Infrastructure"]
        sup["ModelSupervisor"]
        adm["InferenceAdmission"]
        proc["ChildProcesses"]
    end

    main --> listener
    audio --> utt --> listener
    listener --> wake
    listener --> needle
    listener --> router
    router --> needle
    router --> loop --> s2
    router --> ctx
    router --> esc --> s2
    s2 --> harness
    router --> s2
    s2 --> val --> tools
    s2 --> appr --> store
    tools --> wf
    tools --> auto
    sched --> tools
    tools --> store
    router --> ledger --> store
    router --> fb
    harness --> sup
    needle -. admission .-> adm
    harness -. admission .-> adm
    tools --> proc
```

## Layers and dependencies

Dependencies point downward. The one deliberate cycle is the backward-compatibility re-export in
`app.py`, which imports `doctor`, `_main` and `main` from `cli.py` after the `Alfred` class is
defined.

| Layer | Modules | Rule |
|---|---|---|
| Constants and validation | `constants.py`, `validators.py`, `exceptions.py` | No imports from the rest of the package |
| Contracts and persistence | `cognitive/contracts.py`, `cognitive/sessions.py`, `runtime.Store` | Structural metadata only; no transcripts or tool output |
| Tool registry | `runtime.py`, `automations.py`, `workflow_engine.py` | The only code that causes side effects |
| Policy | `system2.py` | Every plan, from Needle or Qwen, goes through the same preflight |
| Model adapters | `models.py`, `backends.py`, `cognitive/harness_*.py` | Return untrusted proposals |
| Orchestration | `router.py`, `listener.py`, `session.py`, `app.py` | Connects the layers above |

See [Module map](../reference/modules.md) for a one-line description of every file.

## Process and thread model

See [Concurrency and shutdown](concurrency.md).

## Where to go next

- [Request flow](request-flow.md): what happens between a spoken sentence and a tool call
- [Cognitive layer](cognitive.md): contracts, sessions, context and escalation
- [DeepSeek Harness](harness.md): how Qwen is hosted and why it has no tools
- [Security model](security-model.md): authority boundaries and fail-closed rules
- [Data model](data-model.md): SQLite tables
