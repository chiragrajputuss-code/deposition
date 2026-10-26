---
name: Verification gave a wrong or surprising result
about: depo verify, depo audit or the viewer said something you think is wrong - including a trace you altered that still passed
title: "Verification: "
---

<!--
Do not attach a trace that contains secrets, API keys or personal data.
Traces record prompts, tool arguments and results verbatim. If yours does,
reduce it to the smallest trace that still shows the problem, or describe
the change you made to it instead of attaching it.
-->

**What you did**

<!-- The command you ran, exactly. For a tampering attempt, what you changed
in the trace and how (edited a body, re-sealed, swapped the .sig, ...). -->

```console

```

**The trace**

<!-- Attach the .jsonl (and .sig / public key if signing is involved), or a
minimal trace that reproduces it. See the warning above. -->

**What you expected**

**What actually happened**

<!-- Paste the full output. -->

```text

```

**Version**

<!-- Output of: python -c "import deposition; print(deposition.__version__)" -->
<!-- and your Python version and OS. -->
