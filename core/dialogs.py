from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Label, Input, Button
from textual.containers import Vertical, Horizontal
import asyncio

class FolderChosen(Message):
    def __init__(self, path: str | None):
        super().__init__()
        self.path = path
class FolderDialog(ModalScreen):
    #CSS_PATH = "wavrun.css"
    def __init__(self, current_folder: str | None): # result_future: asyncio.Future):
        super().__init__()
        self.current_folder = current_folder or ""
    #    self.result_future = result_future

    def compose(self):
        yield Vertical(
            Label("Change Music Folder", id="dlg_title"),
            Input(value=self.current_folder, id="dlg_input"),
            Horizontal(
                Button("Cancel", id="cancel"),
                Button("OK", id="ok", variant="primary"),
                id="dlg_buttons",
            ),
            id="dlg_container",
        )

    def on_mount(self):
        self.query_one("#dlg_input", Input).focus()

    #def _return(self, value):
    #    if not self.result_future.done():
    #        self.result_future.set_result(value)
    #    self.dismiss(value)
    def on_button_pressed(self, event: Button.Pressed):
        input_widget = self.query_one("#dlg_input", Input)
        if event.button.id == "ok":
            self.dismiss(input_widget.value)
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted):
        self.dismiss(event.value)