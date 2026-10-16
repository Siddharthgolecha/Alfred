# Alfred

Offline-first voice automation for the NVIDIA Jetson Orin Nano 8GB.

Alfred listens for the wake word "Alfred", transcribes speech locally with Cactus Whistle, routes
the request with Cactus Needle 3, and executes validated Python tools. Complex requests are
delegated to a local Qwen 3.5 model through the DeepSeek Harness.

:::{note}
Alfred is unit-tested offline but has not been validated on Jetson hardware.
:::

```{toctree}
:maxdepth: 2
:caption: Get started

getting-started/index
```

```{toctree}
:maxdepth: 2
:caption: Do things

guides/index
```

```{toctree}
:maxdepth: 2
:caption: Understand

architecture/index
```

```{toctree}
:maxdepth: 2
:caption: Look up

reference/index
```

```{toctree}
:maxdepth: 2
:caption: Contribute

development/index
```

```{toctree}
:maxdepth: 1
:caption: Jetson-specific

INTRINSIC_JETSON
```
