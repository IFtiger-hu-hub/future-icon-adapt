import QtQuick
import QtQuick.Controls as QQC
import QtQuick.Layouts
import QtQuick.Dialogs
import org.kde.kirigami as Kirigami

Kirigami.ApplicationWindow {
    id: root
    title: "图标适配"
    width: 1200
    height: 800
    minimumWidth: 960
    minimumHeight: 600

    property var apps: []
    property var summary: ({})
    property var strategy: ({})
    property var detail: ({})
    property string selectedId: ""
    property var mask: ({})
    property string feedbackText: ""
    property bool feedbackError: false
    property bool maskOn: false
    property string currentThemeName: ""
    property var modules: []
    property var themeList: []
    property string activeModule: "apps"
    property var focusedPreview: ({})

    function reload() {
        if (typeof iconAdapt === "undefined" || !iconAdapt)
            return
        apps = JSON.parse(iconAdapt.appsJson)
        summary = JSON.parse(iconAdapt.summaryJson)
        strategy = JSON.parse(iconAdapt.strategyJson)
        try { mask = JSON.parse(iconAdapt.maskJson) } catch (e) { mask = {} }
        maskOn = !!(mask && mask.enabled)
        currentThemeName = iconAdapt.currentTheme || ""
        try { modules = JSON.parse(iconAdapt.modulesJson) } catch (e) { modules = [] }
        try { themeList = JSON.parse(iconAdapt.moduleThemeListJson()) } catch (e) { themeList = [] }
        if (selectedId)
            iconAdapt.requestAppDetail(selectedId)
    }

    function statusLabel(s) {
        const map = {
            "ok": "已适配",
            "fallback": "备用图标可用",
            "extra_fallback": "备用图标可用",
            "missing": "缺少图标",
            "absolute_ok": "用的是自带图标",
            "absolute_missing": "自带图标找不到",
            "empty": "还没设置图标"
        }
        return map[s] || s
    }

    function statusHint(s) {
        const map = {
            "ok": "当前图标包里已经有这个应用的图标。",
            "fallback": "当前图标包没有专用图标，但可以用备用的。",
            "extra_fallback": "当前图标包没有专用图标，但可以用系统备用的。",
            "missing": "还没有找到合适的图标，可以在下面挑选一个。",
            "absolute_ok": "这个应用用的是自己带的图标，不一定和主题风格一致。",
            "absolute_missing": "启动器写了自带图标，但文件已经找不到了。",
            "empty": "这个应用没有指定图标。"
        }
        return map[s] || ""
    }

    function showFeedback(msg, isError) {
        feedbackText = msg || ""
        feedbackError = !!isError
    }

    function summaryLine() {
        const t = summary.total || 0
        const miss = summary.missing || 0
        const empty = summary.empty || 0
        const absOk = summary.absolute_ok || 0
        const fallback = (summary.fallback || 0) + (summary.extra_fallback || 0)
        const ok = summary.ok || 0
        return "共 " + t + " 个应用，已适配 " + ok
            + "，缺少图标 " + (miss + empty)
            + "，用的是自带图标 " + absOk
            + "，备用图标可用 " + fallback
    }

    function missingPacksHint() {
        const missing = strategy.missing || []
        if (!missing.length)
            return ""
        return "还没安装 " + missing.join("、") + " 图标包，可以先用已有图标。装上后会更整齐，本工具不会自动下载。"
    }


    function optionLabel(it, mod) {
        if (!it || !it.id)
            return ""
        if (it.system)
            return it.display
        const flags = (mod && mod.pack_flags) ? mod.pack_flags[it.id] : null
        if (flags && flags.kind === "cursors")
            return it.display + "（指针主题，没有这类图标）"
        if (flags && flags.empty)
            return it.display + "（本体没有这类图标）"
        return it.display
    }

    function previewSource(item) {
        if (!item)
            return ""
        if (item.preview_url)
            return item.preview_url
        return ""
    }

    function focusLine(item) {
        if (!item)
            return ""
        const status = item.render_status || "ok"
        const name = item.name || "这个图标"
        if (status === "error" || status === "broken" || status === "missing")
            return "无法预览「" + name + "」：" + (item.render_note || "无法加载")
        if (status === "incomplete") {
            let line = "预览「" + name + "」可能不完整：" + (item.render_note || "源图标不完整")
            if (item.theme) {
                if (item.origin === "fallback")
                    line += "，来自 " + item.theme + "（回退，不是该图标包本体）"
                else if (item.origin === "base")
                    line += "，来自 " + item.theme + "（基础主题）"
                else
                    line += "，来自 " + item.theme
            }
            if (item.size)
                line += " · " + item.size + "px"
            if (item.maybe_unreliable_tray)
                line += "（可能不生效：托盘程序若自己画图标，不会跟着换）"
            return line
        }
        let line = "预览：" + name
        if (item.theme) {
            if (item.origin === "fallback")
                line += "，来自 " + item.theme + "（回退，不是该图标包本体）"
            else if (item.origin === "base")
                line += "，来自 " + item.theme + "（基础主题）"
            else
                line += "，来自 " + item.theme
        }
        if (item.size)
            line += " · " + item.size + "px"
        if (status === "low_contrast")
            line += "。" + (item.render_note || "对比偏低。")
        else if (item.render_note)
            line += "。" + item.render_note
        if (item.maybe_unreliable_tray)
            line += "（可能不生效：托盘程序若自己画图标，不会跟着换）"
        return line
    }


    property bool maskUiReady: false
    property bool advancedOpen: false
    property var launcherPlan: ({})

    function applyMaskFromUi() {
        if (!maskUiReady)
            return
        const transparent = bgTransparentBox.checked
        const payload = {
            enabled: maskEnabledBox.checked,
            shape: shapeBox.currentValue || "circle",
            custom_path: customMaskField.text,
            background: transparent ? "#00000000" : colorToHex(bgSwatch.color),
            padding: padSlider.value
        }
        iconAdapt.setMaskJson(JSON.stringify(payload))
    }

    function colorToHex(c) {
        function h(n) {
            const v = Math.max(0, Math.min(255, Math.round(n * 255)))
            return ("0" + v.toString(16)).slice(-2)
        }
        return "#" + h(c.a) + h(c.r) + h(c.g) + h(c.b)
    }

    function parseBg(hex) {
        const s = (hex || "#00000000").trim()
        return s
    }

    function isTransparentHex(hex) {
        const s = (hex || "").replace("#", "").toLowerCase()
        if (s.length === 8)
            return s.substring(0, 2) === "00"
        if (s.length === 4)
            return s[0] === "0"
        return hex === "transparent"
    }

    function iconNameForSelection() {
        if (root.detail.icon_kind === "absolute") {
            const parts = String(root.detail.icon || "").split("/")
            return String(parts.pop() || "").replace(/\.[^.]+$/, "")
        }
        return root.detail.icon || ""
    }

    function chooseIcon(theme, path, iconName) {
        if (!root.selectedId || !path)
            return
        iconAdapt.saveSelection(root.selectedId, iconName || iconNameForSelection(), theme, path)
    }

    function isChosen(path, theme, iconName) {
        const sel = root.detail.selection
        if (!sel)
            return false
        if (path && sel.source_path === path)
            return true
        if (theme && iconName)
            return sel.source_theme === theme && sel.icon_name === iconName
        return false
    }

    function appliedStatusOf(item) {
        if (item && item.applied_status)
            return item.applied_status
        return ""
    }

    function selectionStateLabel(item) {
        const st = root.appliedStatusOf(item)
        const over = !!(item && item.has_override)
        if (st === "applied")
            return "已应用"
        if (st === "applied_changed" || (over && st !== "applied"))
            return "已应用（选择已改）"
        if (st === "pending" || (item && item.selection) || (item && item.has_selection))
            return "已选择，待应用"
        return ""
    }

    property string restoreTargetId: ""
    property string restoreTargetName: ""

    function confirmRestore(desktopId, appName) {
        if (!desktopId) {
            root.showFeedback("没有可恢复的应用。", true)
            return
        }
        restoreTargetId = desktopId
        restoreTargetName = appName || "这个应用"
        restoreDialog.open()
    }

    function runRestore() {
        const id = root.restoreTargetId
        let r = {}
        try {
            r = JSON.parse(iconAdapt.rollbackApplyToApp(id))
        } catch (e) {
            r = { ok: false, error: "恢复原图标失败。", message: "恢复原图标失败。" }
        }
        root.showFeedback(r.message || r.error || "", !r.ok)
        if (r.ok) {
            restoreDialog.close()
            try { apps = JSON.parse(iconAdapt.appsJson) } catch (e) { }
            if (root.selectedId)
                iconAdapt.requestAppDetail(root.selectedId)
        }
        return r.ok
    }

    function openApplyToApp() {
        if (!root.selectedId) {
            root.showFeedback("请先在左边选一个应用，并点选一个图标。", true)
            return
        }
        try {
            launcherPlan = JSON.parse(iconAdapt.planApplyToApp(root.selectedId))
        } catch (e) {
            launcherPlan = { ok: false, error: "无法准备应用计划。" }
        }
        launcherDialog.open()
    }

    Timer {
        interval: 400
        running: true
        repeat: false
        onTriggered: maskUiReady = true
    }

    Connections {
        target: iconAdapt
        function onDataChanged() { root.reload() }
        function onStatusMessage(msg) { root.showFeedback(msg, false) }
        function onErrorMessage(msg) { root.showFeedback(msg, true) }
        function onAppDetailReady(id, payload) {
            if (id !== root.selectedId)
                return
            try {
                root.detail = JSON.parse(payload)
            } catch (e) {
                root.detail = { desktop_id: id, detail_loading: false, detail_error: "无法解析预览结果" }
            }
        }
        function onSelectionChanged() {
            try { apps = JSON.parse(iconAdapt.appsJson) } catch (e) { }
            if (root.selectedId)
                iconAdapt.requestAppDetail(root.selectedId)
        }
    }

    Component.onCompleted: reload()

    pageStack.initialPage: Kirigami.Page {
        id: mainPage
        title: "图标适配"
        padding: 0
            ColumnLayout {
                anchors.fill: parent
                spacing: 0

                Kirigami.InlineMessage {
                    Layout.fillWidth: true
                    visible: root.feedbackText.length > 0
                    type: root.feedbackError ? Kirigami.MessageType.Error : Kirigami.MessageType.Positive
                    text: root.feedbackText
                    showCloseButton: true
                    onVisibleChanged: if (!visible) root.feedbackText = ""
                }

                Kirigami.InlineMessage {
                    Layout.fillWidth: true
                    visible: (strategy.missing || []).length > 0
                    type: Kirigami.MessageType.Information
                    text: root.missingPacksHint()
                    showCloseButton: true
                }

                QQC.Label {
                    Layout.fillWidth: true
                    Layout.leftMargin: Kirigami.Units.largeSpacing
                    Layout.rightMargin: Kirigami.Units.largeSpacing
                    Layout.topMargin: Kirigami.Units.smallSpacing
                    Layout.bottomMargin: Kirigami.Units.smallSpacing
                    text: "找应用 → 搭配模块 → 预览选择 → 安装主题或应用到这个应用。点选候选只会保存在本工具里，不会立刻改菜单。"
                    opacity: 0.8
                    wrapMode: Text.Wrap
                }

                QQC.SplitView {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    orientation: Qt.Horizontal

                    QQC.Control {
                        QQC.SplitView.preferredWidth: 420
                        QQC.SplitView.minimumWidth: 320
                        padding: Kirigami.Units.largeSpacing
                        contentItem: ColumnLayout {
                            spacing: Kirigami.Units.smallSpacing

                            Kirigami.Heading {
                                level: 2
                                text: "1. 找应用"
                                Layout.fillWidth: true
                            }
                            QQC.Label {
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                text: "系统正在使用「" + root.currentThemeName + "」。" + root.summaryLine() + "。"
                                opacity: 0.85
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Kirigami.Units.smallSpacing
                                QQC.TextField {
                                    id: searchBox
                                    Layout.fillWidth: true
                                    placeholderText: "搜索应用"
                                    onTextChanged: iconAdapt.setQuery(text)
                                }
                                WheelSafeComboBox {
                                    id: filterBox
                                    objectName: "filterBox"
                                    Layout.preferredWidth: 150
                                    model: [
                                        { v: "all", t: "全部" },
                                        { v: "issues", t: "需要处理" },
                                        { v: "ok", t: "已适配" },
                                        { v: "missing", t: "缺少图标" },
                                        { v: "fallback", t: "备用图标可用" },
                                        { v: "absolute_ok", t: "用的是自带图标" },
                                        { v: "absolute_missing", t: "自带图标找不到" }
                                    ]
                                    textRole: "t"
                                    valueRole: "v"
                                    onActivated: iconAdapt.setFilter(currentValue)
                                }
                            }
                            QQC.Label {
                                Layout.fillWidth: true
                                text: root.apps.length ? ("显示 " + root.apps.length + " 个") : "没有符合条件的应用"
                                opacity: 0.7
                                font.pointSize: 9
                            }

                            QQC.ScrollView {
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                ListView {
                                    id: appList
                                    model: root.apps
                                    clip: true
                                    currentIndex: -1
                                    spacing: Kirigami.Units.smallSpacing
                                    delegate: QQC.ItemDelegate {
                                        id: appRow
                                        width: ListView.view.width
                                        highlighted: root.selectedId === modelData.desktop_id
                                        property bool ignoreNextSelect: false
                                        contentItem: ColumnLayout {
                                            spacing: 2
                                            RowLayout {
                                                Layout.fillWidth: true
                                                QQC.Label {
                                                    Layout.fillWidth: true
                                                    text: modelData.name
                                                    elide: Text.ElideRight
                                                    font.bold: root.selectedId === modelData.desktop_id
                                                }
                                                QQC.Label {
                                                    visible: root.selectionStateLabel(modelData).length > 0
                                                    text: root.selectionStateLabel(modelData)
                                                    color: Kirigami.Theme.highlightColor
                                                    font.pointSize: 9
                                                }
                                            }
                                            QQC.Label {
                                                text: root.statusLabel(modelData.status)
                                                opacity: 0.75
                                                font.pointSize: 9
                                                Layout.fillWidth: true
                                            }
                                            QQC.Button {
                                                visible: !!(modelData.has_override)
                                                text: "恢复原图标"
                                                Layout.fillWidth: true
                                                enabled: !iconAdapt.jobBusy
                                                onPressed: appRow.ignoreNextSelect = true
                                                onClicked: root.confirmRestore(modelData.desktop_id, modelData.name)
                                            }
                                        }
                                        onClicked: {
                                            if (appRow.ignoreNextSelect) {
                                                appRow.ignoreNextSelect = false
                                                return
                                            }
                                            appList.currentIndex = index
                                            root.selectedId = modelData.desktop_id
                                            root.detail = {
                                                desktop_id: modelData.desktop_id,
                                                name: modelData.name,
                                                icon: modelData.icon,
                                                icon_kind: modelData.icon_kind,
                                                status: modelData.status,
                                                lookup_path: modelData.lookup_path,
                                                lookup_theme: modelData.lookup_theme,
                                                detail_loading: true,
                                                candidates: [],
                                                hints: []
                                            }
                                            iconAdapt.requestAppDetail(modelData.desktop_id)
                                        }
                                    }
                                }
                            }
                        }
                    }

                    QQC.ScrollView {
                        id: rightPageScroll
                        objectName: "rightPageScroll"
                        QQC.SplitView.fillWidth: true
                        QQC.SplitView.minimumWidth: 440
                        contentWidth: availableWidth
                        ColumnLayout {
                            width: parent ? Math.max(parent.width - Kirigami.Units.largeSpacing * 2, 400) : 400
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.margins: Kirigami.Units.largeSpacing
                            spacing: Kirigami.Units.largeSpacing


                            Kirigami.Heading {
                                level: 2
                                text: "2. 搭配模块"
                                Layout.fillWidth: true
                            }
                            QQC.Label {
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                text: "从已安装的图标包里按类型挑选来源，组合成一套你自己的主题。这是自定义搭配，不会自动统一风格。不想改的一类可以保持系统原样。"
                                opacity: 0.85
                            }

                            Repeater {
                                model: root.modules
                                delegate: ColumnLayout {
                                    id: moduleRow
                                    Layout.fillWidth: true
                                    spacing: Kirigami.Units.smallSpacing
                                    required property var modelData
                                    property var mod: moduleRow.modelData

                                    Kirigami.Separator { Layout.fillWidth: true }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: Kirigami.Units.smallSpacing
                                        QQC.Label {
                                            text: moduleRow.mod.title
                                            font.bold: true
                                            Layout.fillWidth: true
                                        }
                                        QQC.Label {
                                            visible: moduleRow.mod.empty
                                            text: "没有这类图标"
                                            opacity: 0.7
                                        }
                                        QQC.Label {
                                            visible: moduleRow.mod.missing_theme
                                            text: "这个图标包已经找不到"
                                            color: Kirigami.Theme.negativeTextColor
                                        }
                                    }
                                    QQC.Label {
                                        Layout.fillWidth: true
                                        wrapMode: Text.Wrap
                                        text: moduleRow.mod.hint
                                        opacity: 0.8
                                    }
                                    WheelSafeComboBox {
                                        id: srcBox
                                        objectName: "moduleSourceBox"
                                        Layout.fillWidth: true
                                        model: root.themeList
                                        textRole: "display"
                                        valueRole: "id"
                                        currentIndex: {
                                            const opts = root.themeList
                                            const src = moduleRow.mod.source
                                            for (let i = 0; i < opts.length; i++) {
                                                if (opts[i].id === src)
                                                    return i
                                            }
                                            return 0
                                        }
                                        displayText: {
                                            const opts = root.themeList
                                            const it = opts[currentIndex] || {}
                                            if (!it.id)
                                                return moduleRow.mod.source_label
                                            return root.optionLabel(it, moduleRow.mod)
                                        }
                                        onActivated: iconAdapt.setModuleSource(moduleRow.mod.id, currentValue)
                                    }
                                    QQC.Label {
                                        visible: !!(moduleRow.mod.status_line)
                                        Layout.fillWidth: true
                                        wrapMode: Text.Wrap
                                        text: moduleRow.mod.status_line || ""
                                        opacity: 0.8
                                    }
                                    QQC.Label {
                                        visible: !!(moduleRow.mod.preview_caption) && moduleRow.mod.preview_caption !== moduleRow.mod.status_line
                                        Layout.fillWidth: true
                                        wrapMode: Text.Wrap
                                        text: moduleRow.mod.preview_caption || ""
                                        opacity: 0.8
                                    }
                                    Flow {
                                        Layout.fillWidth: true
                                        visible: !!(moduleRow.mod.previews && moduleRow.mod.previews.length)
                                        spacing: Kirigami.Units.smallSpacing
                                        Repeater {
                                            model: moduleRow.mod.previews
                                            delegate: Item {
                                                width: 40
                                                height: 40
                                                required property var modelData
                                                property bool failed: {
                                                    const s = modelData.render_status || "ok"
                                                    return s === "error" || s === "broken" || s === "missing" || thumb.status === Image.Error
                                                }
                                                property bool incomplete: (modelData.render_status || "") === "incomplete"
                                                Rectangle {
                                                    anchors.fill: parent
                                                    radius: 4
                                                    color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.06)
                                                    border.width: 1
                                                    border.color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, failed ? 0.45 : 0.12)
                                                    Image {
                                                        id: thumb
                                                        anchors.fill: parent
                                                        anchors.margins: 3
                                                        source: root.previewSource(modelData)
                                                        fillMode: Image.PreserveAspectFit
                                                        asynchronous: true
                                                        smooth: true
                                                        sourceSize.width: 48
                                                        sourceSize.height: 48
                                                        visible: !failed && status === Image.Ready
                                                    }
                                                    QQC.Label {
                                                        anchors.centerIn: parent
                                                        visible: failed || (thumb.status !== Image.Ready && thumb.status !== Image.Loading && !root.previewSource(modelData))
                                                        text: failed ? "!" : "?"
                                                        font.bold: true
                                                        opacity: 0.7
                                                    }
                                                    QQC.Label {
                                                        anchors.right: parent.right
                                                        anchors.top: parent.top
                                                        anchors.margins: 1
                                                        visible: incomplete && !failed && thumb.status === Image.Ready
                                                        text: "!"
                                                        font.pixelSize: 10
                                                        font.bold: true
                                                        opacity: 0.85
                                                    }
                                                }
                                                MouseArea {
                                                    anchors.fill: parent
                                                    cursorShape: Qt.PointingHandCursor
                                                    onClicked: {
                                                        root.activeModule = moduleRow.mod.id
                                                        root.focusedPreview = modelData
                                                    }
                                                }
                                            }
                                        }
                                    }
                                    QQC.Label {
                                        visible: root.activeModule === moduleRow.mod.id && !!(root.focusedPreview && root.focusedPreview.name)
                                        Layout.fillWidth: true
                                        wrapMode: Text.Wrap
                                        text: root.focusLine(root.focusedPreview)
                                        opacity: 0.85
                                    }
                                    QQC.Label {
                                        visible: !!(moduleRow.mod.note) && (moduleRow.mod.source !== "__system__")
                                        Layout.fillWidth: true
                                        wrapMode: Text.Wrap
                                        text: moduleRow.mod.note || ""
                                        opacity: 0.8
                                    }
                                }
                            }

                            Kirigami.Heading {
                                level: 2
                                text: "3. 为这个应用单独选图标"
                                Layout.fillWidth: true
                            }

                            QQC.Label {
                                visible: !root.selectedId
                                text: "在左边选一个应用，这里会显示可以换用的图标。点一下预览即可选择，不会立刻改系统图标。"
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                                opacity: 0.85
                            }

                            ColumnLayout {
                                visible: !!root.selectedId
                                Layout.fillWidth: true
                                spacing: Kirigami.Units.smallSpacing

                                QQC.Label {
                                    text: detail.name || ""
                                    font.bold: true
                                    wrapMode: Text.Wrap
                                    Layout.fillWidth: true
                                }
                                QQC.Label {
                                    text: root.statusLabel(detail.status || "") + "  " + root.statusHint(detail.status || "")
                                    wrapMode: Text.Wrap
                                    Layout.fillWidth: true
                                    opacity: 0.85
                                }
                                RowLayout {
                                    visible: !!detail.detail_loading
                                    Layout.fillWidth: true
                                    spacing: Kirigami.Units.smallSpacing
                                    QQC.BusyIndicator {
                                        running: !!detail.detail_loading
                                        implicitWidth: Kirigami.Units.iconSizes.small
                                        implicitHeight: Kirigami.Units.iconSizes.small
                                    }
                                    QQC.Label {
                                        text: "正在加载这个应用的图标预览…"
                                        wrapMode: Text.Wrap
                                        Layout.fillWidth: true
                                        opacity: 0.85
                                    }
                                }
                                QQC.Label {
                                    visible: !!(detail.detail_error) && !detail.detail_loading
                                    text: detail.detail_error || ""
                                    wrapMode: Text.Wrap
                                    Layout.fillWidth: true
                                    color: Kirigami.Theme.negativeTextColor
                                }

                                RowLayout {
                                    visible: !!(detail.lookup_path) && (detail.icon_kind !== "absolute" || detail.status === "absolute_ok")
                                    spacing: Kirigami.Units.largeSpacing
                                    Column {
                                        spacing: 4
                                        Image {
                                            width: 64; height: 64
                                            fillMode: Image.PreserveAspectFit
                                            source: detail.lookup_preview_url ? detail.lookup_preview_url : ""
                                            sourceSize.width: 64
                                            sourceSize.height: 64
                                        }
                                        QQC.Label { text: "现在"; opacity: 0.7; anchors.horizontalCenter: parent.horizontalCenter; font.pointSize: 9 }
                                    }
                                    Column {
                                        visible: root.maskOn && !!detail.lookup_path
                                        spacing: 4
                                        Image {
                                            width: 64; height: 64
                                            fillMode: Image.PreserveAspectFit
                                            source: (root.maskOn && detail.lookup_masked_preview_url) ? detail.lookup_masked_preview_url : ""
                                            sourceSize.width: 64
                                            sourceSize.height: 64
                                        }
                                        QQC.Label { text: "统一外形后"; opacity: 0.7; anchors.horizontalCenter: parent.horizontalCenter; font.pointSize: 9 }
                                    }
                                    QQC.Label {
                                        visible: !!(detail.selection)
                                        Layout.fillWidth: true
                                        wrapMode: Text.Wrap
                                        text: root.appliedStatusOf(detail) === "applied"
                                            ? ("已应用「" + ((detail.selection && detail.selection.source_theme) || "") + "」里的图标（只改了这个应用）。")
                                            : ("已选择「" + ((detail.selection && detail.selection.source_theme) || "") + "」里的图标，尚未应用到系统。选好后请点右侧「应用到这个应用」。")
                                    }
                                }

                                QQC.Label {
                                    text: "点选只保存在本工具里，不会立刻改菜单。" + (root.maskOn ? "（左原图，右为统一外形）" : "") + " 选好后请点「应用到这个应用」，或安装整套主题。"
                                    font.bold: true
                                    Layout.fillWidth: true
                                }

                                Flow {
                                    Layout.fillWidth: true
                                    spacing: Kirigami.Units.smallSpacing
                                    Repeater {
                                        model: detail.candidates || []
                                        delegate: QQC.Control {
                                            padding: Kirigami.Units.smallSpacing
                                            background: Rectangle {
                                                radius: Kirigami.Units.smallSpacing
                                                color: root.isChosen(modelData.path, modelData.theme, root.iconNameForSelection())
                                                    ? Qt.rgba(Kirigami.Theme.highlightColor.r, Kirigami.Theme.highlightColor.g, Kirigami.Theme.highlightColor.b, 0.22)
                                                    : (hovered ? Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.06) : "transparent")
                                                border.width: root.isChosen(modelData.path, modelData.theme, root.iconNameForSelection()) ? 2 : 1
                                                border.color: root.isChosen(modelData.path, modelData.theme, root.iconNameForSelection())
                                                    ? Kirigami.Theme.highlightColor
                                                    : Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.18)
                                            }
                                            contentItem: Column {
                                                spacing: 4
                                                width: root.maskOn ? 140 : 88
                                                Row {
                                                    anchors.horizontalCenter: parent.horizontalCenter
                                                    spacing: 4
                                                    Image {
                                                        width: 64; height: 64
                                                        fillMode: Image.PreserveAspectFit
                                                        source: modelData.preview_url ? modelData.preview_url : ""
                                                        sourceSize.width: 64
                                                        sourceSize.height: 64
                                                    }
                                                    Image {
                                                        width: 64; height: 64
                                                        visible: root.maskOn
                                                        fillMode: Image.PreserveAspectFit
                                                        source: (root.maskOn && modelData.masked_preview_url) ? modelData.masked_preview_url : ""
                                                        sourceSize.width: 64
                                                        sourceSize.height: 64
                                                    }
                                                }
                                                QQC.Label {
                                                    width: parent.width
                                                    horizontalAlignment: Text.AlignHCenter
                                                    wrapMode: Text.Wrap
                                                    text: modelData.theme
                                                    font.pointSize: 9
                                                }
                                                QQC.Label {
                                                    width: parent.width
                                                    horizontalAlignment: Text.AlignHCenter
                                                    visible: root.isChosen(modelData.path, modelData.theme, root.iconNameForSelection())
                                                    text: root.appliedStatusOf(detail) === "applied" ? "已应用" : "已选择，待应用"
                                                    color: Kirigami.Theme.highlightColor
                                                    font.pointSize: 9
                                                }
                                            }
                                            HoverHandler { cursorShape: Qt.PointingHandCursor }
                                            MouseArea {
                                                anchors.fill: parent
                                                onClicked: root.chooseIcon(modelData.theme, modelData.path, root.iconNameForSelection())
                                            }
                                        }
                                    }
                                }
                                QQC.Label {
                                    visible: !!root.selectedId && !detail.detail_loading && !(detail.candidates && detail.candidates.length)
                                    text: root.selectedId ? "没有找到完全同名的图标，不会自动乱配。" : ""
                                    opacity: 0.7
                                    Layout.fillWidth: true
                                }

                                QQC.Label {
                                    visible: !!(detail.hints && detail.hints.length)
                                    text: "其他可能的图标"
                                    font.bold: true
                                }
                                Flow {
                                    Layout.fillWidth: true
                                    visible: !!(detail.hints && detail.hints.length)
                                    spacing: Kirigami.Units.smallSpacing
                                    Repeater {
                                        model: detail.hints || []
                                        delegate: QQC.Control {
                                            padding: Kirigami.Units.smallSpacing
                                            background: Rectangle {
                                                radius: Kirigami.Units.smallSpacing
                                                color: root.isChosen(modelData.path, modelData.theme, modelData.icon_name)
                                                    ? Qt.rgba(Kirigami.Theme.highlightColor.r, Kirigami.Theme.highlightColor.g, Kirigami.Theme.highlightColor.b, 0.22)
                                                    : "transparent"
                                                border.width: root.isChosen(modelData.path, modelData.theme, modelData.icon_name) ? 2 : 1
                                                border.color: root.isChosen(modelData.path, modelData.theme, modelData.icon_name)
                                                    ? Kirigami.Theme.highlightColor
                                                    : Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.18)
                                            }
                                            contentItem: RowLayout {
                                                spacing: 8
                                                Image {
                                                    width: 32; height: 32
                                                    fillMode: Image.PreserveAspectFit
                                                    source: modelData.preview_url ? modelData.preview_url : ""
                                                    sourceSize.width: 32
                                                    sourceSize.height: 32
                                                }
                                                QQC.Label {
                                                    text: modelData.theme + " · " + modelData.icon_name
                                                      + (root.isChosen(modelData.path, modelData.theme, modelData.icon_name) ? (root.appliedStatusOf(detail) === "applied" ? "  （已应用）" : "  （已选择，待应用）") : "")
                                                }
                                            }
                                            HoverHandler { cursorShape: Qt.PointingHandCursor }
                                            MouseArea {
                                                anchors.fill: parent
                                                onClicked: root.chooseIcon(modelData.theme, modelData.path, modelData.icon_name)
                                            }
                                        }
                                    }
                                }

                                QQC.Button {
                                    visible: !!(detail.selection)
                                    text: "撤销选择"
                                    onClicked: iconAdapt.clearSelection(root.selectedId)
                                }
                                QQC.Label {
                                    visible: !!(detail.selection)
                                    Layout.fillWidth: true
                                    wrapMode: Text.Wrap
                                    opacity: 0.7
                                    text: "撤销选择只取消本工具里的待应用选择，不会还原已经应用到系统的图标。"
                                }
                            }

                            Kirigami.Separator { Layout.fillWidth: true }

                            Kirigami.Heading {
                                level: 2
                                text: "4. 安装主题"
                                Layout.fillWidth: true
                            }

                            QQC.Label {
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                text: "把搭配好的图标做成一套主题，装到你的用户目录。若系统已经在用这套主题，装完后会尝试刷新桌面图标；否则请到系统设置里手动选用，本工具不会自动切换。"
                                opacity: 0.85
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: Kirigami.Units.smallSpacing

                                QQC.Label {
                                    text: "统一外形"
                                    font.bold: true
                                }
                                QQC.CheckBox {
                                    id: maskEnabledBox
                                    text: "让应用图标外观更统一"
                                    checked: !!(mask.enabled)
                                    onClicked: root.applyMaskFromUi()
                                }
                                QQC.Label {
                                    visible: maskEnabledBox.checked
                                    Layout.fillWidth: true
                                    wrapMode: Text.Wrap
                                    text: "只作用于应用图标，不会裁切文件夹、文件类型、操作和状态图标。开启后安装进去的应用图标会做成普通图片；原来的图标包不会被改动。"
                                    opacity: 0.8
                                }
                                RowLayout {
                                    visible: maskEnabledBox.checked
                                    Layout.fillWidth: true
                                    spacing: Kirigami.Units.largeSpacing
                                    QQC.Label { text: "形状" }
                                    WheelSafeComboBox {
                                        id: shapeBox
                                        objectName: "maskShapeBox"
                                        Layout.preferredWidth: 160
                                        model: [
                                            { v: "circle", t: "圆形" },
                                            { v: "rounded-square", t: "圆角方形" },
                                            { v: "squircle", t: "圆滑方形" },
                                            { v: "custom", t: "自选图形" }
                                        ]
                                        textRole: "t"
                                        valueRole: "v"
                                        onActivated: root.applyMaskFromUi()
                                        Component.onCompleted: {
                                            const cur = mask.shape || "circle"
                                            for (let i = 0; i < count; i++) {
                                                if (model[i].v === cur) { currentIndex = i; break }
                                            }
                                        }
                                    }
                                }
                                RowLayout {
                                    visible: maskEnabledBox.checked
                                    Layout.fillWidth: true
                                    QQC.Label { text: "留白" }
                                    QQC.Label { text: "更满"; opacity: 0.7 }
                                    QQC.Slider {
                                        id: padSlider
                                        Layout.fillWidth: true
                                        from: 0
                                        to: 0.45
                                        stepSize: 0.01
                                        value: (mask.padding !== undefined) ? Number(mask.padding) : 0.08
                                        onPressedChanged: if (!pressed) root.applyMaskFromUi()
                                    }
                                    QQC.Label { text: "更疏"; opacity: 0.7 }
                                }
                                RowLayout {
                                    visible: maskEnabledBox.checked
                                    spacing: Kirigami.Units.smallSpacing
                                    QQC.Label { text: "底板" }
                                    QQC.CheckBox {
                                        id: bgTransparentBox
                                        text: "透明"
                                        checked: root.isTransparentHex(mask.background || "#00000000")
                                        onClicked: root.applyMaskFromUi()
                                    }
                                    Rectangle {
                                        id: bgSwatch
                                        width: 28
                                        height: 28
                                        radius: 4
                                        enabled: !bgTransparentBox.checked
                                        opacity: enabled ? 1 : 0.4
                                        color: root.isTransparentHex(mask.background || "#00000000") ? "#ffffff" : (mask.background || "#ffffffff")
                                        border.width: 1
                                        border.color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.4)
                                        MouseArea {
                                            anchors.fill: parent
                                            enabled: !bgTransparentBox.checked
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: {
                                                bgDialog.selectedColor = bgSwatch.color
                                                bgDialog.open()
                                            }
                                        }
                                    }
                                    QQC.Label {
                                        text: bgTransparentBox.checked ? "不加底色" : "点击色块选颜色"
                                        opacity: 0.7
                                    }
                                }
                                RowLayout {
                                    visible: maskEnabledBox.checked && (shapeBox.currentValue || mask.shape) === "custom"
                                    Layout.fillWidth: true
                                    QQC.TextField {
                                        id: customMaskField
                                        Layout.fillWidth: true
                                        text: mask.custom_path || ""
                                        placeholderText: "选择一个图形文件"
                                        onEditingFinished: root.applyMaskFromUi()
                                    }
                                    QQC.Button {
                                        text: "选择文件…"
                                        onClicked: maskFileDialog.open()
                                    }
                                }
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: Kirigami.Units.smallSpacing
                                visible: iconAdapt.jobBusy || !!iconAdapt.jobLabel
                                QQC.Label {
                                    Layout.fillWidth: true
                                    wrapMode: Text.Wrap
                                    text: iconAdapt.jobLabel || "正在处理…"
                                }
                                QQC.ProgressBar {
                                    Layout.fillWidth: true
                                    visible: iconAdapt.jobBusy
                                    indeterminate: iconAdapt.jobTotal <= 0
                                    from: 0
                                    to: Math.max(1, iconAdapt.jobTotal)
                                    value: iconAdapt.jobDone
                                }
                                QQC.Button {
                                    text: "取消"
                                    visible: iconAdapt.jobBusy
                                    onClicked: iconAdapt.cancelThemeJob()
                                }
                            }

                            RowLayout {
                                spacing: Kirigami.Units.smallSpacing
                                QQC.Button {
                                    text: "安装主题"
                                    highlighted: true
                                    enabled: !iconAdapt.jobBusy
                                    onClicked: iconAdapt.installTheme()
                                }
                                QQC.Button {
                                    text: "卸载主题"
                                    enabled: !iconAdapt.jobBusy
                                    onClicked: iconAdapt.uninstallOutputs()
                                }
                            }
                            QQC.Label {
                                Layout.fillWidth: true
                                wrapMode: Text.Wrap
                                text: "卸载只会删掉本工具安装的主题，原来的图标包不受影响。"
                                opacity: 0.7
                            }

                            QQC.ToolButton {
                                text: root.advancedOpen ? "收起高级选项" : "高级选项"
                                onClicked: root.advancedOpen = !root.advancedOpen
                            }
                            ColumnLayout {
                                visible: root.advancedOpen
                                Layout.fillWidth: true
                                spacing: Kirigami.Units.smallSpacing
                                QQC.Label {
                                    Layout.fillWidth: true
                                    wrapMode: Text.Wrap
                                    text: "安装主题会生成整套图标。若只想立刻改当前这一个应用，用下面的「应用到这个应用」。点选候选不会自动写入系统。"
                                    opacity: 0.8
                                }
                                QQC.Button {
                                    text: "只生成预览，不安装"
                                    enabled: !iconAdapt.jobBusy
                                    onClicked: iconAdapt.generateTheme()
                                }
                                QQC.Button {
                                    text: "应用到这个应用…"
                                    enabled: !!root.selectedId && !iconAdapt.jobBusy
                                    onClicked: root.openApplyToApp()
                                }
                                QQC.Button {
                                    text: "添加到应用菜单"
                                    onClicked: {
                                        let r = {}
                                        try { r = JSON.parse(iconAdapt.installSelfLauncher()) }
                                        catch (e) { r = { ok: false, error: "无法添加启动器。", message: "无法添加启动器。" } }
                                        root.showFeedback(r.message || r.error || "", !r.ok)
                                    }
                                }
                                QQC.Button {
                                    text: "从应用菜单移除"
                                    onClicked: {
                                        let r = {}
                                        try { r = JSON.parse(iconAdapt.removeSelfLauncher()) }
                                        catch (e) { r = { ok: false, error: "无法移除启动器。", message: "无法移除启动器。" } }
                                        root.showFeedback(r.message || r.error || "", !r.ok)
                                    }
                                }
                                QQC.Label {
                                    Layout.fillWidth: true
                                    wrapMode: Text.Wrap
                                    opacity: 0.7
                                    text: "菜单名称是「图标适配」。只安装本工具自己的启动器，不会改其他应用。若以后移动了项目目录，请再添加一次。"
                                }
                            }
                            Item { Layout.fillHeight: true; Layout.preferredHeight: Kirigami.Units.largeSpacing }
                        }
                    }
                }
            }

            FileDialog {
                id: maskFileDialog
                title: "选择外形图形（SVG 或 PNG）"
                nameFilters: ["图像 (*.svg *.svgz *.png)", "全部文件 (*)"]
                fileMode: FileDialog.OpenFile
                onAccepted: {
                    let u = selectedFile ? String(selectedFile) : ""
                    if (u.startsWith("file://"))
                        u = decodeURIComponent(u.substring(7))
                    customMaskField.text = u
                    root.applyMaskFromUi()
                }
            }

            ColorDialog {
                id: bgDialog
                title: "选择底板颜色"
                onAccepted: {
                    bgTransparentBox.checked = false
                    bgSwatch.color = selectedColor
                    root.applyMaskFromUi()
                }
            }

            QQC.Dialog {
                id: launcherDialog
                title: "应用到这个应用"
                modal: true
                standardButtons: QQC.Dialog.NoButton
                width: 480
                anchors.centerIn: parent
                contentItem: ColumnLayout {
                    spacing: Kirigami.Units.largeSpacing
                    QQC.Label {
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        text: root.launcherPlan && root.launcherPlan.ok
                            ? (root.launcherPlan.summary || root.launcherPlan.message || "将只把当前选中的图标应用到这个应用。")
                            : ((root.launcherPlan && root.launcherPlan.error) ? root.launcherPlan.error : "请先在上面点选一个图标。")
                    }
                    RowLayout {
                        spacing: Kirigami.Units.smallSpacing
                        QQC.Button {
                            text: "确认应用"
                            highlighted: true
                            enabled: !!(root.launcherPlan && root.launcherPlan.ok) && !iconAdapt.jobBusy
                            onClicked: {
                                let r = {}
                                try {
                                    r = JSON.parse(iconAdapt.applyToApp(root.selectedId))
                                } catch (e) {
                                    r = { ok: false, error: "应用失败。", message: "应用失败。" }
                                }
                                const msg = r.message || r.error || ""
                                root.showFeedback(msg, !r.ok)
                                if (r.ok)
                                    launcherDialog.close()
                                else
                                    root.launcherPlan = r
                            }
                        }
                        QQC.Button {
                            text: "取消"
                            onClicked: launcherDialog.close()
                        }
                        Item { Layout.fillWidth: true }
                        QQC.Button {
                            text: "恢复原图标"
                            enabled: !iconAdapt.jobBusy
                            onClicked: {
                                launcherDialog.close()
                                root.confirmRestore(root.selectedId, detail.name || "这个应用")
                            }
                        }
                    }
                }
            }

            QQC.Dialog {
                id: restoreDialog
                title: "恢复原图标"
                modal: true
                standardButtons: QQC.Dialog.NoButton
                width: 420
                anchors.centerIn: parent
                contentItem: ColumnLayout {
                    spacing: Kirigami.Units.largeSpacing
                    QQC.Label {
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        text: "将恢复「" + (root.restoreTargetName || "这个应用") + "」的原图标，不会取消已选候选。"
                    }
                    RowLayout {
                        spacing: Kirigami.Units.smallSpacing
                        QQC.Button {
                            text: "确认恢复"
                            highlighted: true
                            enabled: !iconAdapt.jobBusy
                            onClicked: root.runRestore()
                        }
                        QQC.Button {
                            text: "取消"
                            onClicked: restoreDialog.close()
                        }
                    }
                }
            }
    }
}
