import QtQuick
import QtQuick.Controls as QQC

// ComboBox that does not change the current item when the popup is closed
// and the user scrolls with the wheel. Closed-state wheel is forwarded to
// an ancestor Flickable (e.g. a page ScrollView). An open popup keeps its
// normal list scrolling and click-to-select. Keyboard is left unchanged.
//
// Two style-specific paths exist in real Plasma sessions:
//
// 1) org.kde.desktop (qqc2-desktop-style) sets wheelEnabled: true, so
//    QQuickComboBox::wheelEvent increment/decrements the index. Force
//    wheelEnabled off so that C++ path stays disarmed.
//
// 2) org.kde.breeze (the style this session actually loads via the KDE
//    platform theme) leaves wheelEnabled false, but installs its own
//    WheelHandler { target: control } that calls incrementCurrentIndex /
//    decrementCurrentIndex while the popup is closed. A child WheelHandler
//    and wheelEnabled: false do not stop that handler. sendEvent() to the
//    ComboBox item also misses it; window/pointer delivery is the path
//    that matches a real hover wheel. Disarm foreign WheelHandlers on
//    this control and no-op the QML increment/decrement entry points
//    while collapsed.
QQC.ComboBox {
    id: control
    wheelEnabled: false
    onWheelEnabledChanged: if (control.wheelEnabled) control.wheelEnabled = false

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

    function disarmForeignWheelHandlers() {
        control.wheelEnabled = false
        function consider(obj) {
            if (!obj || obj === closedWheelGate)
                return
            if (obj instanceof WheelHandler) {
                obj.enabled = false
                if (obj.enabledChanged && !obj._iconAdaptWheelDisarmed) {
                    obj._iconAdaptWheelDisarmed = true
                    obj.enabledChanged.connect(function() {
                        if (obj.enabled && !control.popupIsOpen())
                            obj.enabled = false
                    })
                }
            }
        }
        if (control.data) {
            for (let i = 0; i < control.data.length; ++i)
                consider(control.data[i])
        }
        if (control.children) {
            for (let i = 0; i < control.children.length; ++i)
                consider(control.children[i])
        }
    }

    // Shadow the QML-visible invokables used by org.kde.breeze's style
    // WheelHandler. C++ key handling still calls the C++ methods directly,
    // so keyboard changes/opens stay intact.
    function incrementCurrentIndex() {
        // Closed: ignore QML/style wheel. Open: the popup ListView scrolls
        // itself; breeze already disables its handler when the popup is visible.
        if (!control.popupIsOpen())
            return
    }
    function decrementCurrentIndex() {
        if (!control.popupIsOpen())
            return
    }

    Component.onCompleted: {
        control.wheelEnabled = false
        disarmForeignWheelHandlers()
        Qt.callLater(disarmForeignWheelHandlers)
    }
    onCountChanged: Qt.callLater(disarmForeignWheelHandlers)

    WheelHandler {
        id: closedWheelGate
        objectName: "iconAdaptClosedWheelGate"
        // Intercept only while collapsed, so an open popup list still scrolls.
        enabled: !control.popupIsOpen()
        acceptedDevices: PointerDevice.AllDevices
        // Take the grab if needed, but do not yield it to the style handler.
        grabPermissions: PointerHandler.CanTakeOverFromAnything
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
            event.accepted = true
        }
    }
}
