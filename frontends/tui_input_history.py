class InputHistoryMixin:
    """Small history navigator for Textual TextArea subclasses."""

    def _init_input_history(self) -> None:
        self._history: list[str] = []
        self._history_index: int | None = None
        self._history_draft = ""

    def add_history(self, value: str) -> None:
        value = (value or "").rstrip()
        if not value:
            self._history_index = None
            self._history_draft = ""
            return
        if not self._history or self._history[-1] != value:
            self._history.append(value)
        self._history_index = None
        self._history_draft = ""

    def show_previous_history(self) -> bool:
        if not self._history:
            return False
        if self._history_index is None:
            self._history_draft = self.text
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        self._set_history_text(self._history[self._history_index])
        return True

    def show_next_history(self) -> bool:
        if self._history_index is None:
            return False
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._set_history_text(self._history[self._history_index])
            return True
        self._history_index = None
        self._set_history_text(self._history_draft)
        self._history_draft = ""
        return True

    def _set_history_text(self, value: str) -> None:
        self.text = value
        lines = value.split("\n")
        try:
            self.move_cursor((len(lines) - 1, len(lines[-1])))
        except Exception:
            pass
