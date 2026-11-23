import QtQuick
import QtQuick.Controls as QQC

// ComboBox that does not change the current item when the popup is closed
// and the user scrolls with the wheel. Closed-state wheel is forwarded to
// an ancestor Flickable (e.g. a page ScrollView). An open popup keeps its
// normal list scrolling and click-to-select. Keyboard is left unchanged.
//
// org.kde.desktop (qqc2-desktop-style) sets wheelEnabled: true on ComboBox.
// QQuickComboBox::wheelEvent then calls increment/decrementCurrentIndex and
// emits activated whenever the popup is closed. A child WheelHandler does
// not reliably stop that C++ path: window-targeted synthetic events can be
// eaten by the handler, but delivery to the ComboBox item (the real hover
// path) still runs wheelEvent. Force wheelEnabled off so the style cannot
// leave the C++ path armed.
QQC.ComboBox {
    id: control
    wheelEnabled: false

    function ancestorFlickable() {
        let p = control.parent
        while (p) {
            if (p.flickableDirection !== undefined && p.contentY !== undefined)
                return p
            if (p.contentItem && p.contentItem.flickableDirection !== undefined
                    && p.contentItem.contentY !== undefined)
                return p.contentItem
            p = p.parent
        }
        return null
    }

    function popupIsOpen() {
        return !!(control.down || (control.popup && control.popup.opened))
    }

    Component.onCompleted: control.wheelEnabled = false

    WheelHandler {
        // Intercept only while collapsed, so an open popup list still scrolls.
        enabled: !control.popupIsOpen()
        // Do not narrow to Mouse|TouchPad only: some backends report a
        // generic or stylus/touchscreen device for the same physical wheel.
        acceptedDevices: PointerDevice.AllDevices
        grabPermissions: PointerHandler.CanTakeOverFromAnything | PointerHandler.ApprovesTakeOverByAnything
        onWheel: (event) => {
            const f = control.ancestorFlickable()
            if (f && f.interactive !== false) {
                let dy = event.pixelDelta.y
                if (!dy)
                    dy = event.angleDelta.y
                const maxY = Math.max(0, f.contentHeight - f.height)
                f.contentY = Math.max(0, Math.min(maxY, f.contentY - dy))
                event.accepted = true
                return
            }
            // No scrollable ancestor: still swallow so a leftover style
            // handler cannot change the closed-box value.
            event.accepted = true
        }
    }
}
