"""The control desk: a window with a big switch for the listener.

The desk itself is the web page in ui/panel.html, shown in a WebKit view.
Its buttons post a message ("toggle", "sync", "playlist") to the app, and the
app pushes the listener's state back by calling window.setState(...).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

PAGE = Path(__file__).resolve().parent / "ui" / "panel.html"
HANDLER_NAME = "tracker"   # window.webkit.messageHandlers.<this> in the page
TITLE = "TeknoRadio"
SIZE = (760, 720)
STATE_KEYS = ("listening", "tracks", "in_playlist", "last_track", "message", "recent")


def state_script(state: dict) -> str:
    """The JavaScript that shows `state` (a Controller snapshot) on the desk."""
    data = {key: state.get(key) for key in STATE_KEYS}
    return f"window.setState && window.setState({json.dumps(data)});"


def dispatch(action: str, handlers: dict[str, Callable[[], None]]) -> bool:
    """Run the handler for a button the page reported; unknown actions are ignored."""
    handler = handlers.get(action)
    if handler is None:
        return False
    handler()
    return True


_bridge = None


def _bridge_class():
    """The Objective-C object WebKit hands the page's messages to. Defined
    once, because an Objective-C class name can only be registered once."""
    global _bridge
    if _bridge is None:
        import objc
        import WebKit  # noqa: F401  (loads the WKScriptMessageHandler protocol)
        from Foundation import NSObject

        class SputnikDeskBridge(NSObject, protocols=[objc.protocolNamed("WKScriptMessageHandler")]):
            def userContentController_didReceiveScriptMessage_(self, controller, message):
                self.on_message(str(message.body()))

        _bridge = SputnikDeskBridge
    return _bridge


class Panel:
    """The desk window. Create it once the app is running; closing the window
    only hides it, and show() brings it back."""

    def __init__(self, handlers: dict[str, Callable[[], None]]):
        from AppKit import (NSBackingStoreBuffered, NSWindow, NSWindowStyleMaskClosable,
                            NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskTitled)
        from Foundation import NSURL, NSMakeRect
        from WebKit import WKWebView, WKWebViewConfiguration

        self._handlers = handlers
        self._ready = False      # the page has loaded and can take setState()
        self._state: dict | None = None

        self._bridge = _bridge_class().alloc().init()
        self._bridge.on_message = self._on_message
        config = WKWebViewConfiguration.alloc().init()
        config.userContentController().addScriptMessageHandler_name_(self._bridge, HANDLER_NAME)

        width, height = SIZE
        frame = NSMakeRect(0, 0, width, height)
        self.web = WKWebView.alloc().initWithFrame_configuration_(frame, config)
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False)
        self.window.setTitle_(TITLE)
        self.window.setReleasedWhenClosed_(False)
        self.window.setContentView_(self.web)
        self.window.center()

        url = NSURL.fileURLWithPath_(str(PAGE))
        self.web.loadFileURL_allowingReadAccessToURL_(url, url.URLByDeletingLastPathComponent())

    def _on_message(self, action: str) -> None:
        if action == "ready":
            self._ready = True
            self._push()
        else:
            dispatch(action, self._handlers)

    def _push(self) -> None:
        if self._ready and self._state is not None:
            self.web.evaluateJavaScript_completionHandler_(state_script(self._state), None)

    def update(self, state: dict) -> None:
        self._state = state
        self._push()

    def show(self) -> None:
        from AppKit import NSApplication

        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
