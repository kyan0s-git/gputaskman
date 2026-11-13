"""The update dialog (``u``): checks only when opened, installs only when asked."""

from __future__ import annotations

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from . import update


class UpdateScreen(ModalScreen):
    BINDINGS = [
        Binding("escape,q,u", "app.pop_screen", "Close"),
        Binding("i", "install", "Install"),
    ]
    DEFAULT_CSS = """
    UpdateScreen { align: center middle; }
    UpdateScreen > Vertical { width: 90; max-width: 95%; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    #update-body { height: auto; }
    #update-keys { height: 1; color: $text-muted; margin-top: 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.status: update.UpdateStatus | None = None
        self.busy = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(f"Checking {update.RELEASES_PAGE} …"), id="update-body")
            yield Static("Esc close", id="update-keys")

    def on_mount(self) -> None:
        self.query_one(Vertical).border_title = "Update"
        self.check()

    def say(self, text: Text | str, keys: str = "Esc close") -> None:
        self.query_one("#update-body", Static).update(text)
        self.query_one("#update-keys", Static).update(keys)

    @work(thread=True, exclusive=True)
    def check(self) -> None:
        try:
            status = update.check()
        except update.UpdateError as error:
            self.app.call_from_thread(self.say, Text(f"Update check failed: {error}", style="yellow"))
            return
        self.app.call_from_thread(self.show_status, status)

    def show_status(self, status: update.UpdateStatus) -> None:
        self.status = status
        text = Text()
        text.append(status.summary + "\n", style="bold green" if status.newer else "bold")
        if status.newer and status.latest:
            notes = status.latest.notes.strip()
            if notes:
                text.append("\n" + (notes[:800] + (" …" if len(notes) > 800 else "")) + "\n", style="grey70")
            text.append("\n" + update.how_to_upgrade(status), style="cyan")
            text.append(f"\n{status.latest.url}", style="grey50")
        keys = "i install · Esc close" if status.newer and status.kind == "installer" else "Esc close"
        self.say(text, keys)

    def action_install(self) -> None:
        if self.busy or not self.status or not self.status.newer or self.status.kind != "installer":
            return
        self.busy = True
        self.install(self.status.latest)

    @work(thread=True, exclusive=True)
    def install(self, release: update.Release) -> None:
        def progress(done: int, total: int) -> None:
            pct = f" ({100 * done / total:.0f}%)" if total else ""
            self.app.call_from_thread(self.say, Text(f"Downloading {update.installer_name(release.version)}{pct}"),
                                      "please wait")

        try:
            path = update.download_installer(release, progress=progress)
            self.app.call_from_thread(self.say, Text(f"Verified SHA-256. Starting the installer; gputaskman will "
                                                     f"close and reopen as {release.version}.", style="green"), "")
            update.launch_installer(path)
        except update.UpdateError as error:
            self.busy = False
            self.app.call_from_thread(self.say, Text(f"Update failed: {error}", style="red"), "Esc close")
            return
        self.app.call_from_thread(self.app.exit)
