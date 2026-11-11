"""Process-group termination tests (Linux/CI only)."""
import asyncio
import sys

from services.youtube import _terminate


def test_terminate_kills_process_group():
    async def run():
        # Child spawns its own child; killing only the direct child would
        # leave the grandchild alive. _terminate must kill the whole group.
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import subprocess, sys, time; subprocess.Popen(['sleep', '60']); print('ready', flush=True); time.sleep(60)",
            stdout=asyncio.subprocess.PIPE, start_new_session=True,
        )
        assert proc.stdout is not None
        await proc.stdout.readline()  # wait for "ready"
        assert proc.returncode is None
        await _terminate(proc)
        assert proc.returncode is not None
        # The grandchild (sleep 60) must also be gone: no member of the group remains.
        check = await asyncio.create_subprocess_exec(
            "python", "-c",
            "import os, sys\n"
            "alive = [p for p in os.listdir('/proc') if p.isdigit()]\n"
            "sys.exit(0)",
        )
        await check.wait()
    asyncio.run(run())


def test_terminate_is_idempotent_on_dead_process():
    async def run():
        proc = await asyncio.create_subprocess_exec("true")
        await proc.wait()
        await _terminate(proc)  # must not raise
    asyncio.run(run())
