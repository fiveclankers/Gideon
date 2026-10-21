import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland

// GIDEON Quickshell Orb & Subtitles Overlay
// Native Wayland layer-shell 3D GPU plasma orb + frosted smoked glass telemetry
// Theme: Boreal Pine, Bioluminescent Canopy & Claude-style Claude Gerund Telemetry

ShellRoot {
    id: root

    // ---- Configuration ------------------------------------------------------
    property string socketPath: "/tmp/gideon-ui.sock"
    property bool   anchorBottom: false     // false = top-right, true = bottom-right
    property int    marginSide: 8           // distance from screen right edge
    property int    marginEdge: 16          // clearance below Omarchy top-bar
    property string monitorName: ""         // "" = primary display
    property int    windowWidth: 260
    property int    windowHeight: 260

    // ---- Live State ---------------------------------------------------------
    property string orbState: "idle"
    property string stateDetail: ""
    property real   rmsTarget: 0.0
    property real   lastRmsMs: 0
    property double lastMsgMs: 0 // proof-of-life: updated on every socket line

    // Claude Code-style dynamic gerunds for thinking/conversing
    readonly property var gerundList: [
        "Discombobulating",
        "Perambulating",
        "Cogitating",
        "Ruminating",
        "Pondering",
        "Synthesizing",
        "Deliberating",
        "Contemplating",
        "Osmosing",
        "Transmogrifying",
        "Pontificating",
        "Deciphering",
        "Unraveling",
        "Mulling",
        "Speculating",
        "Calibrating",
        "Navigating",
        "Brewing",
        "Conjuring",
        "Extrapolating"
    ]
    property int gerundIndex: 0
    readonly property string currentGerund: gerundList[gerundIndex]

    // Rotate whimsical gerunds every 1.8s during thinking/conversing
    Timer {
        id: gerundTimer
        interval: 1800
        running: root.orbState === "thinking" || root.orbState === "conversing"
        repeat: true
        onTriggered: {
            root.gerundIndex = (root.gerundIndex + 1) % root.gerundList.length
        }
    }

    // Dock orb to edge when idle
    readonly property bool isHiding: root.orbState === "idle"

    // ---- Visual & Mode Presets ----------------------------------------------
    // Boreal Pine, Radiant Emerald, Bioluminescent Lichen, Electric Sky & Amber Canopy
    readonly property var presets: ({
        idle: {
            diam: 70, morph: 0.00, spin: 0.25, hueSpeed: 0.02, speak: 0, hot: 0,
            c1: "#0e3d2f", c2: "#092218", c3: "#03130d",
            label: "", ringMode: "none", accent: "#34d399",
            badgeBg: Qt.rgba(0.04, 0.07, 0.05, 0.84),
            badgeBorder: Qt.rgba(0.2, 0.8, 0.5, 0.22)
        },
        listening: {
            diam: 96, morph: 0.35, spin: 0.60, hueSpeed: 0.03, speak: 0, hot: 0,
            c1: "#10b981", c2: "#a7f3d0", c3: "#064e3b",
            label: "Listening…", ringMode: "ripple", accent: "#6ee7b7",
            badgeBg: Qt.rgba(0.02, 0.15, 0.10, 0.88),
            badgeBorder: Qt.rgba(0.2, 0.9, 0.6, 0.35)
        },
        thinking: {
            diam: 92, morph: 0.82, spin: 3.40, hueSpeed: 0.38, speak: 0, hot: 0,
            c1: "#047857", c2: "#6ee7b7", c3: "#0f2922",
            label: "🧠 Thinking…", ringMode: "orbit", accent: "#a7f3d0",
            badgeBg: Qt.rgba(0.03, 0.14, 0.10, 0.90),
            badgeBorder: Qt.rgba(0.4, 0.9, 0.7, 0.40)
        },
        conversing: {
            diam: 94, morph: 0.85, spin: 3.30, hueSpeed: 0.40, speak: 0, hot: 0,
            c1: "#4f46e5", c2: "#9333ea", c3: "#1e1b4b",
            label: "🧠 LLM Conversing", ringMode: "orbit", accent: "#c084fc",
            badgeBg: Qt.rgba(0.12, 0.08, 0.24, 0.92),
            badgeBorder: Qt.rgba(0.7, 0.4, 1.0, 0.45)
        },
        bge_reflex: {
            diam: 98, morph: 0.60, spin: 2.80, hueSpeed: 0.28, speak: 0, hot: 0,
            c1: "#059669", c2: "#06b6d4", c3: "#022c22",
            label: "⚡ BGE Reflex", ringMode: "scan", accent: "#34d399",
            badgeBg: Qt.rgba(0.02, 0.18, 0.14, 0.94),
            badgeBorder: Qt.rgba(0.2, 0.95, 0.65, 0.55)
        },
        toolcall: {
            diam: 92, morph: 0.65, spin: 2.90, hueSpeed: 0.18, speak: 0, hot: 0.25,
            c1: "#d97706", c2: "#15803d", c3: "#451a03",
            label: "Executing tool…", ringMode: "scan", accent: "#fbbf24",
            badgeBg: Qt.rgba(0.18, 0.10, 0.02, 0.92),
            badgeBorder: Qt.rgba(0.95, 0.7, 0.2, 0.45)
        },
        speaking: {
            diam: 104, morph: 1.00, spin: 1.80, hueSpeed: 0.12, speak: 1, hot: 0,
            c1: "#0284c7", c2: "#10b981", c3: "#082f49",
            label: "Speaking…", ringMode: "speech", accent: "#38bdf8",
            badgeBg: Qt.rgba(0.03, 0.12, 0.20, 0.92),
            badgeBorder: Qt.rgba(0.2, 0.75, 0.95, 0.45)
        },
        hotmic: {
            diam: 84, morph: 0.20, spin: 0.40, hueSpeed: 0.03, speak: 0, hot: 1,
            c1: "#16a34a", c2: "#84cc16", c3: "#14532d",
            label: "Open mic", ringMode: "beacon", accent: "#eab308",
            badgeBg: Qt.rgba(0.10, 0.14, 0.04, 0.88),
            badgeBorder: Qt.rgba(0.8, 0.85, 0.2, 0.40)
        }
    })

    readonly property var activePreset: presets[orbState] ? presets[orbState] : presets.idle

    readonly property string effectiveStatusLabel: {
        const d = String(stateDetail).trim()
        if (d.length > 0) {
            // Priority 1: BGE reflex badge
            if (d.indexOf("BGE") !== -1 || d.indexOf("⚡") !== -1) {
                return d
            }
            // Priority 2: LLM formulating or synthesizing speech
            if (d.indexOf("LLM: Formulating") !== -1 || d.indexOf("LLM: Synthesizing") !== -1 || d.indexOf("Synthesizing") !== -1) {
                return d
            }
            // Priority 3: LLM thinking with rotating Claude gerund
            if (d.indexOf("LLM") !== -1 && (d.indexOf("Thinking") !== -1 || d.indexOf("Routing") !== -1)) {
                return "🧠 LLM: " + currentGerund + "…"
            }
            return d
        }
        if (orbState === "conversing") {
            return "🧠 LLM: " + currentGerund + "…"
        }
        if (orbState === "thinking") {
            return "🧠 " + currentGerund + "…"
        }
        if (orbState === "speaking") {
            return "Speaking…"
        }
        return activePreset.label
    }

    function normalizeState(rawVal) {
        if (!rawVal) return "idle"
        const s = String(rawVal).toLowerCase()
        if (s.indexOf("bge") !== -1 || s.indexOf("reflex") !== -1 || s === "tier1") {
            return "bge_reflex"
        }
        if (s.indexOf("convers") !== -1 || s.indexOf("chat") !== -1 || s.indexOf("llm") !== -1) {
            return "conversing"
        }
        if (s === "tool_call" || s === "toolcall" || s === "tool" || s === "executing" || s === "command" || s === "action" || s === "search" || s === "web_search" || s === "browsing") {
            return "toolcall"
        }
        return presets[s] ? s : "idle"
    }

    function setState(name, detail) {
        const norm = normalizeState(name)
        orbState = norm
        stateDetail = detail ? String(detail) : ""
        if (norm === "idle" || norm === "thinking" || norm === "conversing" || norm === "hotmic" || norm === "toolcall" || norm === "bge_reflex") {
            rmsTarget = 0.0
        }
    }

    function handleLine(line) {
        root.lastMsgMs = Date.now()
        let msg
        try { msg = JSON.parse(line.trim()) } catch (e) { return }
        if (msg.event === "state") {
            setState(msg.val, msg.detail)
        } else if (msg.event === "rms") {
            const v = Number(msg.val)
            rmsTarget = isNaN(v) ? 0.0 : Math.max(0.0, Math.min(1.0, v))
            lastRmsMs = Date.now()
        }
    }

    // ---- Socket Client ------------------------------------------------------
    property bool socketAlive: true
    property bool socketResetArmed: false
    readonly property var sock: sockLoader.item
    readonly property bool sockLive: sock !== null && sock !== undefined && sock.connected

    Loader {
        id: sockLoader
        active: root.socketAlive
        sourceComponent: sockComponent
    }

    Component {
        id: sockComponent
        Socket {
            path: root.socketPath
            connected: true
            parser: SplitParser {
                splitMarker: "\n"
                onRead: data => root.handleLine(data)
            }
        }
    }

    // Resilient Reconnect
    Timer {
        id: reconnectTimer
        interval: 1500
        running: true
        repeat: true
        onTriggered: {
            const staleMs = Date.now() - root.lastMsgMs
            if (!root.sockLive || staleMs > 6000) {
                if (!root.socketResetArmed) {
                    root.socketAlive = false
                    root.socketResetArmed = true
                } else {
                    root.socketAlive = true
                    root.socketResetArmed = false
                }
            } else if (root.socketResetArmed) {
                root.socketResetArmed = false
                if (!root.socketAlive) root.socketAlive = true
            }
        }
    }

    // ---- Window Overlay -----------------------------------------------------
    PanelWindow {
        id: win

        screen: {
            const list = Quickshell.screens
            if (root.monitorName !== "") {
                for (let i = 0; i < list.length; i++)
                    if (list[i].name === root.monitorName) return list[i]
            }
            return list.length > 0 ? list[0] : null
        }

        anchors {
            right: true
            top: !root.anchorBottom
            bottom: root.anchorBottom
        }
        margins {
            right: root.marginSide
            top: root.marginEdge
            bottom: root.marginEdge
        }

        implicitWidth: root.windowWidth
        implicitHeight: root.windowHeight
        color: "transparent"

        exclusionMode: ExclusionMode.Ignore
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
        WlrLayershell.namespace: "gideon-orb"

        mask: Region {
            item: orbHitbox
        }

        Item {
            id: container
            anchors.fill: parent

            // -----------------------------------------------------------------
            // 1. THE 3D ORB CONTAINER
            // -----------------------------------------------------------------
            Item {
                id: orb
                anchors.top: parent.top
                anchors.topMargin: 4
                anchors.right: parent.right
                anchors.rightMargin: 4
                width: 220
                height: 220
                transformOrigin: Item.Center

                // When idle, tucks snugly right into the top-right corner
                property real slideX: root.isHiding ? 74 : 0
                property real slideY: root.isHiding ? -12 : 0
                Behavior on slideX {
                    NumberAnimation {
                        duration: 700
                        easing.type: Easing.InOutCubic
                    }
                }
                Behavior on slideY {
                    NumberAnimation {
                        duration: 700
                        easing.type: Easing.InOutCubic
                    }
                }
                transform: Translate { x: orb.slideX; y: orb.slideY }

                // Soft resting opacity when idle; bright when active
                property real activeOpacity: root.isHiding ? 0.32 : 1.0
                Behavior on activeOpacity {
                    NumberAnimation {
                        duration: 550
                        easing.type: Easing.InOutQuad
                    }
                }

                // Scales down to subtle standby dot in the corner when idle
                property real activeScale: root.isHiding ? 0.44 : 1.0
                Behavior on activeScale {
                    NumberAnimation {
                        duration: 700
                        easing.type: root.isHiding ? Easing.InBack : Easing.OutBack
                        easing.overshoot: 1.15
                    }
                }

                // Interactive click pop-out / expansion variables
                property real clickScale: 1.0
                property real clickOpacity: 1.0

                // Shader uniforms & easing declaratively bound to activePreset
                property real diam: root.activePreset.diam
                property real morph: root.activePreset.morph
                property real spin: root.activePreset.spin
                property real hueSpeed: root.activePreset.hueSpeed
                property real speak: root.activePreset.speak
                property real hot: root.activePreset.hot
                property color c1: root.activePreset.c1
                property color c2: root.activePreset.c2
                property color c3: root.activePreset.c3

                // Per-frame integrators
                property real time: 0
                property real swirl: 0
                property real hue: 0
                property real rms: 0
                property real appear: 0

                readonly property bool online: root.sockLive

                Behavior on diam     { NumberAnimation { duration: 650; easing.type: Easing.InOutCubic } }
                Behavior on morph    { NumberAnimation { duration: 700; easing.type: Easing.InOutQuad } }
                Behavior on spin     { NumberAnimation { duration: 900; easing.type: Easing.InOutQuad } }
                Behavior on hueSpeed { NumberAnimation { duration: 900 } }
                Behavior on speak    { NumberAnimation { duration: 400 } }
                Behavior on hot      { NumberAnimation { duration: 450; easing.type: Easing.InOutQuad } }
                Behavior on c1 { ColorAnimation { duration: 700 } }
                Behavior on c2 { ColorAnimation { duration: 700 } }
                Behavior on c3 { ColorAnimation { duration: 700 } }

                onOnlineChanged: {
                    if (online && appear < 0.01) appearAnim.restart()
                }

                NumberAnimation {
                    id: appearAnim
                    target: orb
                    property: "appear"
                    from: 0
                    to: 1
                    duration: 1200
                    easing.type: Easing.OutCubic
                }

                Component.onCompleted: {
                    if (online) appearAnim.restart()
                }

                // Click tap-to-expand animation
                SequentialAnimation {
                    id: tapExpandAnim
                    running: false
                    ParallelAnimation {
                        NumberAnimation {
                            target: orb
                            property: "clickScale"
                            to: 1.45
                            duration: 180
                            easing.type: Easing.OutQuad
                        }
                        NumberAnimation {
                            target: orb
                            property: "clickOpacity"
                            to: 0.0
                            duration: 220
                            easing.type: Easing.InQuad
                        }
                    }
                    ScriptAction {
                        script: {
                            Qt.openUrlExternally("http://localhost:8790/faces/board/")
                        }
                    }
                    PauseAnimation { duration: 320 }
                    ParallelAnimation {
                        NumberAnimation {
                            target: orb
                            property: "clickScale"
                            from: 0.55
                            to: 1.0
                            duration: 500
                            easing.type: Easing.OutBack
                            easing.overshoot: 1.2
                        }
                        NumberAnimation {
                            target: orb
                            property: "clickOpacity"
                            from: 0.0
                            to: 1.0
                            duration: 450
                            easing.type: Easing.OutQuad
                        }
                    }
                }

                // Frame-rate driven dynamics
                FrameAnimation {
                    running: true
                    onTriggered: {
                        const dt = Math.min(frameTime, 0.05)
                        orb.time += dt

                        // Spin & swirl integration
                        const currentSpin = orb.spin * (1.0 + orb.rms * 1.8)
                        orb.swirl += dt * currentSpin

                        // Continuous seamless hue progression (pal wraps at 3.0)
                        orb.hue = (orb.hue + dt * orb.hueSpeed) % 3.0

                        // Smooth audio RMS response
                        const now = Date.now()
                        const target = (now - root.lastRmsMs > 350) ? 0.0 : root.rmsTarget
                        const attack = 0.35
                        const decay = 0.12
                        const coef = target > orb.rms ? attack : decay
                        orb.rms += (target - orb.rms) * coef
                    }
                }

                // Visual telemetry rings
                Item {
                    id: ringsContainer
                    anchors.centerIn: parent
                    width: parent.width
                    height: parent.height
                    transformOrigin: Item.Center
                    scale: orb.activeScale * orb.clickScale
                    opacity: root.isHiding ? 0.0 : (orb.appear * (root.activePreset.ringMode === "none" ? 0.0 : 0.90)) * orb.clickOpacity
                    Behavior on opacity { NumberAnimation { duration: 400 } }

                    // Ripple ring for listening state
                    Rectangle {
                        id: rippleRing
                        anchors.centerIn: parent
                        width: 120 + orb.rms * 50
                        height: width
                        radius: width * 0.5
                        color: "transparent"
                        border.width: 1.5
                        border.color: root.activePreset.accent
                        opacity: root.activePreset.ringMode === "ripple" ? (0.35 + 0.55 * orb.rms) : 0.0
                        Behavior on width { NumberAnimation { duration: 60 } }
                    }

                    // Orbiting satellite ring for thinking/conversing/scan states
                    Item {
                        id: orbitSpinner
                        anchors.centerIn: parent
                        width: 132
                        height: 132
                        visible: root.activePreset.ringMode === "orbit" || root.activePreset.ringMode === "scan"

                        RotationAnimation on rotation {
                            from: 0
                            to: 360
                            duration: root.activePreset.ringMode === "scan" ? 1400 : 2000
                            loops: Animation.Infinite
                            running: orbitSpinner.visible
                        }

                        Rectangle {
                            width: root.activePreset.ringMode === "scan" ? 22 : 7
                            height: 4
                            radius: 2
                            color: root.activePreset.accent
                            anchors.horizontalCenter: parent.horizontalCenter
                            anchors.top: parent.top
                            anchors.topMargin: -2
                        }
                    }

                    // Concentric Acoustic Wave Equalizer Rings for SPEAKING state
                    Item {
                        id: speechAcousticRings
                        anchors.centerIn: parent
                        width: parent.width
                        height: parent.height
                        visible: root.activePreset.ringMode === "speech"

                        // Inner pulsating voice ripple
                        Rectangle {
                            anchors.centerIn: parent
                            width: 114 + orb.rms * 36 + Math.sin(orb.time * 6.0) * 4
                            height: width
                            radius: width * 0.5
                            color: "transparent"
                            border.width: 2.0
                            border.color: "#38bdf8"
                            opacity: 0.40 + 0.55 * orb.rms
                            Behavior on width { NumberAnimation { duration: 45 } }
                        }

                        // Middle harmonic wave ring
                        Rectangle {
                            anchors.centerIn: parent
                            width: 138 + orb.rms * 62 + Math.cos(orb.time * 5.0) * 6
                            height: width
                            radius: width * 0.5
                            color: "transparent"
                            border.width: 1.5
                            border.color: "#06b6d4"
                            opacity: 0.25 + 0.60 * orb.rms
                            Behavior on width { NumberAnimation { duration: 55 } }
                        }

                        // Outer radiant aura wave
                        Rectangle {
                            anchors.centerIn: parent
                            width: 164 + orb.rms * 84 + Math.sin(orb.time * 4.0) * 8
                            height: width
                            radius: width * 0.5
                            color: "transparent"
                            border.width: 1.0
                            border.color: "#34d399"
                            opacity: 0.15 + 0.50 * orb.rms
                            Behavior on width { NumberAnimation { duration: 65 } }
                        }
                    }
                }

                // Native Qt 6 ShaderEffect
                ShaderEffect {
                    id: fx
                    anchors.fill: parent

                    property real u_time: orb.time
                    property real u_rms: orb.rms
                    property real u_state_morph: orb.morph
                    property real u_swirl: orb.swirl
                    property real u_hue: orb.hue
                    property real u_radius: {
                        const breathe = Math.sin(orb.time * 1.1) * 0.03 * (1.0 - orb.morph)
                        const pulse = orb.rms * 0.12 * (1.0 - orb.speak) + (orb.speak * orb.rms * 0.16)
                        return (orb.diam * 0.5 * (1.0 + breathe + pulse)) / (width * 0.5)
                    }
                    property real u_speak: orb.speak
                    property real u_hotmic: orb.hot
                    property color u_color1: orb.c1
                    property color u_color2: orb.c2
                    property color u_color3: orb.c3

                    fragmentShader: Qt.resolvedUrl("OrbShader.frag.qsb")

                    transformOrigin: Item.Center
                    scale: (0.55 + 0.45 * orb.appear) * orb.activeScale * orb.clickScale
                    opacity: Math.min(1.0, orb.appear) * orb.activeOpacity * orb.clickOpacity * (orb.online ? 1.0 : 0.85)
                    Behavior on opacity { NumberAnimation { duration: 400 } }
                }

                // Smoked glass status badge at bottom of orb
                Rectangle {
                    id: stateBadge
                    readonly property bool showBadge: !root.isHiding && root.effectiveStatusLabel.length > 0 && orb.clickOpacity > 0.5
                    anchors.bottom: parent.bottom
                    anchors.bottomMargin: 4
                    anchors.horizontalCenter: parent.horizontalCenter
                    height: 24
                    width: Math.max(80, badgeRow.implicitWidth + 20)
                    radius: 12
                    color: root.activePreset.badgeBg ? root.activePreset.badgeBg : Qt.rgba(0.04, 0.07, 0.05, 0.88)
                    border.width: 1
                    border.color: root.activePreset.badgeBorder ? root.activePreset.badgeBorder : Qt.rgba(0.2, 0.8, 0.5, 0.30)

                    opacity: showBadge ? 1.0 : 0.0
                    scale: showBadge ? 1.0 : 0.88
                    Behavior on opacity { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
                    Behavior on scale   { NumberAnimation { duration: 220; easing.type: Easing.OutBack } }
                    Behavior on width   { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }
                    Behavior on color   { ColorAnimation { duration: 300 } }
                    Behavior on border.color { ColorAnimation { duration: 300 } }

                    Row {
                        id: badgeRow
                        anchors.centerIn: parent
                        spacing: 7

                        // Mode indicator: mini audio visualizer bars when speaking, pulsing dot otherwise
                        Item {
                            width: root.orbState === "speaking" ? 14 : 7
                            height: 10
                            anchors.verticalCenter: parent.verticalCenter

                            // Single pulsing dot for non-speaking states
                            Rectangle {
                                anchors.centerIn: parent
                                width: 7
                                height: 7
                                radius: 3.5
                                color: root.activePreset.accent
                                visible: root.orbState !== "speaking"

                                SequentialAnimation on opacity {
                                    running: stateBadge.showBadge && root.orbState !== "speaking"
                                    loops: Animation.Infinite
                                    NumberAnimation { from: 1.0; to: 0.35; duration: 700; easing.type: Easing.InOutSine }
                                    NumberAnimation { from: 0.35; to: 1.0; duration: 700; easing.type: Easing.InOutSine }
                                }
                            }

                            // 3 mini dancing EQ bars when speaking
                            Row {
                                anchors.fill: parent
                                spacing: 2
                                visible: root.orbState === "speaking"

                                Rectangle {
                                    width: 3
                                    height: Math.max(3, Math.min(10, 4 + orb.rms * 6 + Math.sin(orb.time * 9.0) * 3))
                                    radius: 1.5
                                    color: "#38bdf8"
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Rectangle {
                                    width: 3
                                    height: Math.max(4, Math.min(10, 5 + orb.rms * 5 + Math.cos(orb.time * 11.0) * 4))
                                    radius: 1.5
                                    color: "#06b6d4"
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                Rectangle {
                                    width: 3
                                    height: Math.max(3, Math.min(10, 3 + orb.rms * 7 + Math.sin(orb.time * 8.0) * 3))
                                    radius: 1.5
                                    color: "#34d399"
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }
                        }

                        Text {
                            id: statusLabelText
                            text: root.effectiveStatusLabel
                            color: "#F8FAFC"
                            font.family: "Inter, Roboto, sans-serif"
                            font.pixelSize: 11
                            font.weight: Font.DemiBold
                            anchors.verticalCenter: parent.verticalCenter
                        }
                    }
                }

                // Interactive Click Area (Masked specifically for tap-to-expand)
                Item {
                    id: orbHitbox
                    anchors.centerIn: parent
                    width: 120
                    height: 120

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: {
                            if (!tapExpandAnim.running) {
                                tapExpandAnim.restart()
                            }
                        }
                    }
                }
            }
        }
    }
}
