# XController

A personal Playwright experiment for X.com, using a persistent Chromium profile.

**Not actively maintained.** The code includes reading, posting, engagement and cleanup workflows, but X's changing interface may require repairs. These are browser actions, not guaranteed API transactions. Use only accounts and actions you are authorized to control.

## Install

Python 3.10+, from the repository directory:

```bash
python -m pip install -e .
playwright install chromium
```

## Read-only example

Uses an already signed-in profile. If login is needed, it exits without taking any account action.

```python
import asyncio
from XController import XController

async def main():
    controller = XController(profile_path="data/profiles/default_profile")
    await controller.start()
    try:
        if not await controller.is_logged_in():
            print("Sign in with this browser profile before running the example.")
            return
        posts = await controller.search_posts("browser automation", limit=5)
        for post in posts:
            print(post.platform_post_id)
    finally:
        await controller.close()

asyncio.run(main())
```

## Package

- `XController` / `XTextAdapter`: async controller.
- `SyncXController` / `XControllerService`: synchronous facade.
- `ControllerSettings`: browser and diagnostic options.
- `controller.last_action_error`: details of the latest soft UI failure.
- `strict_ui_failures=True`: raise `UIActionError` instead of returning a soft failure.

[`main`](https://github.com/mortal414/XController) contains the controller package. The former `labui-testing` branch is no longer available; older branch-workflow notes are historical.

[API reference](docs/API.md) · [Architecture](docs/ARCHITECTURE.md) · [Change log](CHANGELOG.md)
