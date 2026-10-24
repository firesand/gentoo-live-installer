"""The options of a repair, drawn from the parameter list the engine publishes for the action (docs: ActionSpec.params)."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QVBoxLayout, QWidget

from ..widgets import FormGrid, wrapped_label
from . import model

# Words for the values the engine uses; anything else is shown as it is.
CHOICE_TEXTS = {
    "": "Automatic", "auto": "Automatic", "uefi": "UEFI", "bios": "Legacy BIOS", "yes": "Yes", "no": "No", "grub": "GRUB", "systemd-boot": "systemd-boot",
    "check": "Check only (changes nothing)", "repair": "Repair the problems it finds", "repair_force": "Repair and accept data loss (last resort)",
    "config": "Rebuild with the kernel configuration", "remerge": "Build the kernel package again",
}


class OptionsForm(QWidget):
    """One input per parameter. ``values()`` gives the request's ``params``; ``missing()`` names required inputs that are empty."""

    def __init__(self, spec: dict[str, Any], kernels: list[str], devices: list[str], root_device: str, defaults: dict[str, Any] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        self.params = model.params_of(spec)
        self.fields: dict[str, QWidget] = {}
        self.kernel_boxes: dict[str, QCheckBox] = {}
        defaults = defaults or {}
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(wrapped_label(str(spec.get("description", "")), muted=True))
        grid = FormGrid()
        column.addLayout(grid)
        row = 0
        for param in self.params:
            if param.type == "object":
                continue  # a table of per-line choices: the engine's own proposal is used and shown in the plan
            widget = self._make(param, kernels, devices, root_device, defaults.get(param.name, param.default))
            label = param.label + (" (required)" if param.required else "")
            if isinstance(widget, QCheckBox):
                grid.addWidget(widget, row, 0, 1, 2)
            else:
                grid.add_field(row, label, widget)
            self.fields[param.name] = widget
            row += 2 if not isinstance(widget, QCheckBox) else 1

    def _make(self, param: model.Param, kernels: list[str], devices: list[str], root_device: str, default: Any) -> QWidget:
        if param.name in {"username", "bootnum"}:  # filled with the system's users or the firmware's entries when the answer arrives; typing stays possible
            box = QComboBox()
            box.setEditable(True)
            box.setAccessibleName(param.label)
            return box
        if param.type == "enum":
            box = QComboBox()
            for choice in param.choices:
                box.addItem(CHOICE_TEXTS.get(choice, choice), choice)
            box.setCurrentIndex(max(0, box.findData("" if default is None else str(default))))
            box.setAccessibleName(param.label)
            return box
        if param.type == "bool":
            check = QCheckBox(param.label)
            check.setChecked(bool(default))
            return check
        if param.type == "device":
            box = QComboBox()
            box.addItem("Automatic" if param.name != "device" else "The system's own disk", "")
            for device in devices:
                box.addItem(device, device)
            box.setCurrentIndex(max(0, box.findData(root_device if param.name == "device" and default in {None, ""} else str(default or ""))))
            box.setAccessibleName(param.label)
            return box
        if param.type == "list" and param.name == "kernels" and kernels:
            holder = QWidget()
            inner = QVBoxLayout(holder)
            inner.setContentsMargins(0, 0, 0, 0)
            inner.addWidget(wrapped_label("Leave all unticked for every kernel.", muted=True))
            for version in kernels:
                check = QCheckBox(version)
                check.setChecked(version in (default or []))
                self.kernel_boxes[version] = check
                inner.addWidget(check)
            return holder
        edit = QLineEdit()
        edit.setAccessibleName(param.label)
        if param.type == "list":
            edit.setText(",".join(default or []) if isinstance(default, list) else str(default or ""))
            edit.setPlaceholderText("Separate the entries with commas")
        else:
            edit.setText("" if default is None else str(default))
        return edit

    # ---- filled in when an answer arrives --------------------------------------------------------------------------------------------
    def offer_users(self, users: list[dict[str, Any]]) -> None:
        """The login users of the system as a list to pick from (the field stays editable)."""
        field = self.fields.get("username")
        if isinstance(field, QComboBox):
            for user in users:
                field.addItem(str(user["name"]), str(user["name"]))
            field.setCurrentIndex(0 if users else -1)

    def offer_boot_entries(self, state: dict[str, Any]) -> None:
        """The firmware's entries to pick from for a single entry, and the current order for the order field."""
        field = self.fields.get("bootnum")
        if isinstance(field, QComboBox):
            for entry in state.get("entries", []):
                if entry.get("number"):
                    field.addItem(f"{entry['number']} — {entry.get('label', '')} {entry.get('loader', '')}".strip(), str(entry["number"]))
            field.setCurrentIndex(0 if field.count() else -1)
        order = self.fields.get("order")
        if isinstance(order, QLineEdit):
            order.setText(",".join(str(item) for item in state.get("order", [])))

    # ---- reading the inputs ---------------------------------------------------------------------------------------------------------
    def values(self) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for param in self.params:
            field = self.fields.get(param.name)
            if field is None:
                continue
            value: Any
            if isinstance(field, QCheckBox):
                value = field.isChecked()
            elif isinstance(field, QComboBox) and field.isEditable():
                position = field.findText(field.currentText())
                value = field.itemData(position) if position >= 0 and field.itemData(position) else field.currentText().strip()
            elif isinstance(field, QComboBox):
                value = field.currentData()
            elif isinstance(field, QLineEdit):
                value = [part.strip() for part in field.text().split(",") if part.strip()] if param.type == "list" else field.text().strip()
            elif param.name == "kernels":
                value = [version for version, box in self.kernel_boxes.items() if box.isChecked()]
            else:
                continue
            if value in ("", None) or (param.type == "list" and not value):
                if not param.required:
                    continue
            found[param.name] = value
        return found

    def missing(self) -> list[str]:
        values = self.values()
        return [param.label for param in self.params if param.required and param.type != "object" and values.get(param.name) in (None, "", [])]

