from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


ACCENT = "#8b7cf6"
ACCENT_DARK = "#6554c0"
TEXT = "#24222c"
MUTED = "#6d6979"
SURFACE = "#ffffff"
BACKGROUND = "#f5f4f8"
BORDER = "#dfdce7"


def apply_theme(app: QApplication) -> None:
    global TEXT, MUTED, SURFACE, BACKGROUND, BORDER
    if app.styleHints().colorScheme() == Qt.ColorScheme.Dark:
        TEXT = "#ece9f2"
        MUTED = "#aaa4b5"
        SURFACE = "#292631"
        BACKGROUND = "#1d1b23"
        BORDER = "#45404f"
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BACKGROUND))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(SURFACE))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(BACKGROUND))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(SURFACE))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(MUTED))
    app.setPalette(palette)
    app.setStyleSheet(f"""
        QWidget {{ color: {TEXT}; font-family: 'Noto Sans', 'DejaVu Sans', sans-serif; font-size: 13px; }}
        QMainWindow, QWidget#appRoot {{ background: {BACKGROUND}; }}
        QFrame#sidebar {{ background: #211d2e; border: 0; }}
        QFrame#sidebar QLabel {{ color: #c9c4d8; }}
        QFrame#sidebar QLabel#brand {{ color: white; font-size: 20px; font-weight: 700; }}
        QFrame#sidebar QLabel#brandMark {{ color: white; background: {ACCENT_DARK}; border-radius: 20px; font-size: 22px; font-weight: 700; }}
        QPushButton#stepButton {{ color: #bdb6cf; background: transparent; border: 0; border-radius: 8px; padding: 10px 12px; text-align: left; }}
        QPushButton#stepButton:hover {{ background: #302a42; color: white; }}
        QPushButton#stepButton[current=true] {{ background: #3b3351; color: white; font-weight: 600; }}
        QPushButton#stepButton:disabled {{ color: #716a83; }}
        QFrame#contentHeader {{ background: {BACKGROUND}; border-bottom: 1px solid {BORDER}; }}
        QLabel#pageTitle {{ font-size: 25px; font-weight: 700; }}
        QLabel#pageSubtitle, QLabel[muted=true] {{ color: {MUTED}; }}
        QLabel#sectionTitle {{ font-size: 17px; font-weight: 650; }}
        QLabel#heroTitle {{ font-size: 32px; font-weight: 750; }}
        QLabel#heroText {{ font-size: 16px; color: {MUTED}; }}
        QFrame#card, QFrame#choiceCard, QFrame#statusCard {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px; }}
        QFrame#choiceCard[selected=true] {{ border: 2px solid {ACCENT}; background: {BACKGROUND}; }}
        QFrame#choiceCard:hover {{ border-color: {ACCENT}; }}
        QLabel#cardTitle {{ font-size: 15px; font-weight: 650; }}
        QLabel#cardValue {{ font-size: 19px; font-weight: 650; }}
        QPushButton {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; padding: 9px 16px; min-height: 20px; }}
        QPushButton:hover {{ border-color: {ACCENT}; background: {BACKGROUND}; }}
        QPushButton:disabled {{ color: {MUTED}; background: {BACKGROUND}; }}
        QPushButton#primaryButton {{ color: white; background: {ACCENT_DARK}; border: 0; font-weight: 600; padding: 10px 22px; }}
        QPushButton#primaryButton:hover {{ background: #5544aa; }}
        QPushButton#dangerButton {{ color: white; background: #b83a4b; border: 0; font-weight: 600; }}
        QPushButton#linkButton {{ color: {ACCENT}; background: transparent; border: 0; padding: 4px; text-align: left; }}
        QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit, QTableWidget {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 7px; padding: 7px; selection-background-color: {ACCENT}; }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{ border: 2px solid {ACCENT}; }}
        QComboBox::drop-down {{ border: 0; width: 26px; }}
        QCheckBox, QRadioButton {{ spacing: 8px; }}
        QCheckBox::indicator, QRadioButton::indicator {{ width: 17px; height: 17px; }}
        QProgressBar {{ background: {BORDER}; border: 0; border-radius: 6px; height: 12px; text-align: center; }}
        QProgressBar::chunk {{ background: {ACCENT}; border-radius: 6px; }}
        QScrollArea {{ background: transparent; border: 0; }}
        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: #c9c4d2; border-radius: 4px; min-height: 30px; }}
        QToolTip {{ color: white; background: #302b3a; border: 0; padding: 6px; }}
        QTabBar::tab {{ padding: 9px 14px; }}
        QTabBar::tab:selected {{ color: {ACCENT_DARK}; font-weight: 600; }}
    """)
