import QtQuick 2.6

Item {
    id: screensaver

    property bool playing: false
    property bool liteMode: false
    property bool isPartial: false
    property string guideString: ""

    property string slidesDirUrl: "file:///media/developer/echoframe/slides/"
    property string apiUrl: "http://127.0.0.1:18663/api/state"

    property var fallbackSlides: /*__FALLBACK_SLIDES__*/[]
    property var slides: []
    property int currentIndex: 0
    property bool showingA: true
    property int intervalMs: 15000
    property bool showCaptions: true
    property bool showClock: true
    property bool remotePaused: false
    property int serverRevision: -1
    property int lastCommandSeq: 0

    property string activeCaption: ""
    property string activeMeta: ""
    property color ambientTint: "#121721"
    property string clockText: ""

    signal looped()

    anchors.fill: parent

    FontLoader {
        id: serifFont
        source: "file:///media/developer/echoframe/fonts/InstrumentSerif-Regular.ttf"
    }

    FontLoader {
        id: sansFont
        source: "file:///media/developer/echoframe/fonts/Manrope-Variable.ttf"
    }

    Rectangle {
        id: bgBase
        anchors.fill: parent
        color: "#080a0f"
    }

    Rectangle {
        id: bgWash
        anchors.fill: parent
        color: screensaver.ambientTint
        opacity: 0.34
        Behavior on color {
            ColorAnimation { duration: 900; easing.type: Easing.OutCubic }
        }
    }

    Image {
        id: slotA
        anchors.fill: parent
        asynchronous: true
        cache: false
        smooth: true
        fillMode: Image.PreserveAspectFit
        sourceSize.width: screensaver.width > 0 ? screensaver.width : 1920
        sourceSize.height: screensaver.height > 0 ? screensaver.height : 1080
        opacity: 0
        z: screensaver.showingA ? 2 : 1

        Behavior on opacity {
            NumberAnimation {
                duration: 850
                easing.type: Easing.OutCubic
                onRunningChanged: {
                    if (!running && slotA.opacity === 0 && !screensaver.showingA) {
                        slotA.source = "";
                    }
                }
            }
        }

        onStatusChanged: {
            if (status === Image.Ready && screensaver.showingA) {
                slotA.opacity = 1;
                slotB.opacity = 0;
            } else if (status === Image.Error && screensaver.showingA && screensaver.slides.length > 1) {
                advanceSlide(1);
            }
        }
    }

    Image {
        id: slotB
        anchors.fill: parent
        asynchronous: true
        cache: false
        smooth: true
        fillMode: Image.PreserveAspectFit
        sourceSize.width: screensaver.width > 0 ? screensaver.width : 1920
        sourceSize.height: screensaver.height > 0 ? screensaver.height : 1080
        opacity: 0
        z: screensaver.showingA ? 1 : 2

        Behavior on opacity {
            NumberAnimation {
                duration: 850
                easing.type: Easing.OutCubic
                onRunningChanged: {
                    if (!running && slotB.opacity === 0 && screensaver.showingA) {
                        slotB.source = "";
                    }
                }
            }
        }

        onStatusChanged: {
            if (status === Image.Ready && !screensaver.showingA) {
                slotB.opacity = 1;
                slotA.opacity = 0;
            } else if (status === Image.Error && !screensaver.showingA && screensaver.slides.length > 1) {
                advanceSlide(1);
            }
        }
    }

    Rectangle {
        id: bottomScrim
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: Math.round(parent.height * 0.24)
        z: 5
        visible: screensaver.showCaptions && (screensaver.activeCaption.length > 0 || screensaver.activeMeta.length > 0)
        gradient: Gradient {
            GradientStop { position: 0.0; color: "#0006080c" }
            GradientStop { position: 0.55; color: "#6606080c" }
            GradientStop { position: 1.0; color: "#cc06080c" }
        }
    }

    Column {
        id: captionPlate
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.leftMargin: 56
        anchors.rightMargin: 56
        anchors.bottomMargin: 42
        spacing: 8
        z: 6
        visible: screensaver.showCaptions && (screensaver.activeCaption.length > 0 || screensaver.activeMeta.length > 0)

        Text {
            id: captionTitle
            width: Math.min(parent.width, 1180)
            text: screensaver.activeCaption
            color: "#f5f2eb"
            font.family: serifFont.status === FontLoader.Ready ? serifFont.name : "Droid Serif"
            font.pixelSize: 40
            wrapMode: Text.WordWrap
            maximumLineCount: 2
            elide: Text.ElideRight
            style: Text.Raised
            styleColor: "#99000000"
        }

        Text {
            id: captionMeta
            width: Math.min(parent.width, 1180)
            text: screensaver.activeMeta
            color: "#d4cfc3"
            font.family: sansFont.status === FontLoader.Ready ? sansFont.name : "LG Smart UI"
            font.pixelSize: 21
            font.letterSpacing: 0.6
            visible: text.length > 0
            style: Text.Raised
            styleColor: "#99000000"
        }
    }

    Rectangle {
        id: clockPill
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.topMargin: 36
        anchors.rightMargin: 44
        width: clockLabel.implicitWidth + 36
        height: clockLabel.implicitHeight + 18
        radius: height / 2
        color: "#780b0e13"
        border.color: "#33f5f2eb"
        border.width: 1
        z: 6
        visible: screensaver.showClock && screensaver.clockText.length > 0

        Text {
            id: clockLabel
            anchors.centerIn: parent
            text: screensaver.clockText
            color: "#f5f2eb"
            font.family: sansFont.status === FontLoader.Ready ? sansFont.name : "LG Smart UI"
            font.pixelSize: 24
            font.weight: Font.Medium
        }
    }

    Timer {
        id: slideTimer
        interval: screensaver.intervalMs
        repeat: true
        running: screensaver.playing && !screensaver.remotePaused && screensaver.slides.length > 1
        onTriggered: advanceSlide(1)
    }

    Timer {
        id: clockTimer
        interval: 15000
        repeat: true
        running: screensaver.playing
        onTriggered: updateClock()
    }

    Timer {
        id: syncTimer
        interval: 15000
        repeat: true
        running: screensaver.playing
        onTriggered: pollServerState()
    }

    function updateClock() {
        var now = new Date();
        var h = now.getHours();
        var m = now.getMinutes();
        var ampm = h >= 12 ? "PM" : "AM";
        h = h % 12;
        if (h === 0) h = 12;
        clockText = h + ":" + (m < 10 ? "0" + m : m) + " " + ampm;
    }

    function shuffledCopy(list) {
        var arr = [];
        for (var i = 0; i < list.length; i++) {
            arr.push(list[i]);
        }
        for (var j = arr.length - 1; j > 0; j--) {
            var k = Math.floor(Math.random() * (j + 1));
            var tmp = arr[j];
            arr[j] = arr[k];
            arr[k] = tmp;
        }
        return arr;
    }

    function slideFileUrl(slide) {
        if (!slide) return "";
        if (slide.file_url) return slide.file_url;
        if (slide.filename) return slidesDirUrl + slide.filename;
        if (slide.id) return slidesDirUrl + slide.id + ".jpg";
        return "";
    }

    function renderCurrent() {
        if (!slides || slides.length === 0) return;
        if (currentIndex < 0 || currentIndex >= slides.length) currentIndex = 0;
        var s = slides[currentIndex];
        var nextUrl = slideFileUrl(s);
        if (!nextUrl) return;

        activeCaption = s.display_caption || s.label || "";
        var metaParts = [];
        if (s.date_label) metaParts.push(s.date_label);
        if (s.age_badge) metaParts.push(s.age_badge);
        activeMeta = metaParts.join("  ·  ");

        var rgb = s.accent_rgb;
        if (rgb && rgb.length >= 3) {
            ambientTint = Qt.rgba(
                Math.min(255, Math.max(0, rgb[0])) / 255.0,
                Math.min(255, Math.max(0, rgb[1])) / 255.0,
                Math.min(255, Math.max(0, rgb[2])) / 255.0,
                1.0
            );
        } else {
            ambientTint = "#121721";
        }

        showingA = !showingA;
        var target = showingA ? slotA : slotB;
        var other = showingA ? slotB : slotA;

        if (target.source.toString() === nextUrl && target.status === Image.Ready) {
            target.opacity = 1;
            other.opacity = 0;
        } else {
            target.opacity = 0;
            target.source = nextUrl;
        }
    }

    function advanceSlide(delta) {
        if (!slides || slides.length === 0) return;
        currentIndex = (currentIndex + delta + slides.length) % slides.length;
        renderCurrent();
    }

    function applyServerState(st) {
        if (!st || !st.slides || !st.slides.length) return;
        var prevRev = serverRevision;
        serverRevision = st.revision || 0;

        intervalMs = Math.max(5000, (st.interval_sec || 15) * 1000);
        showCaptions = st.show_captions !== false;
        showClock = st.show_clock !== false;
        remotePaused = Boolean(st.paused);

        var curId = (slides.length && slides[currentIndex]) ? slides[currentIndex].id : "";
        var nextSlides = (prevRev === -1) ? shuffledCopy(st.slides) : st.slides;

        var listChanged = (nextSlides.length !== slides.length);
        if (!listChanged && nextSlides.length > 0 && slides.length > 0) {
            listChanged = (nextSlides[0].id !== slides[0].id);
        }

        var cmd = st.command || {};
        var hasNewCmd = cmd.seq && cmd.seq !== lastCommandSeq;
        if (hasNewCmd) lastCommandSeq = cmd.seq;

        if (listChanged || prevRev === -1) {
            slides = nextSlides;
            if (curId && prevRev !== -1) {
                var idx = 0;
                for (var i = 0; i < slides.length; i++) {
                    if (slides[i].id === curId) { idx = i; break; }
                }
                currentIndex = idx;
            } else {
                currentIndex = 0;
            }
            renderCurrent();
        }

        if (hasNewCmd) {
            if (cmd.action === "next") {
                advanceSlide(1);
                slideTimer.restart();
            } else if (cmd.action === "prev") {
                advanceSlide(-1);
                slideTimer.restart();
            } else if (cmd.action === "goto" && cmd.slide_id) {
                for (var j = 0; j < slides.length; j++) {
                    if (slides[j].id === cmd.slide_id) {
                        currentIndex = j;
                        renderCurrent();
                        slideTimer.restart();
                        break;
                    }
                }
            }
        }
    }

    function pollServerState() {
        var cur = (slides.length && slides[currentIndex]) ? slides[currentIndex] : null;
        var qs = "?role=screensaver";
        if (cur && cur.id) qs += "&slide=" + encodeURIComponent(cur.id);
        if (cur && cur.display_caption) qs += "&caption=" + encodeURIComponent(cur.display_caption);

        var xhr = new XMLHttpRequest();
        xhr.open("GET", apiUrl + qs, true);
        xhr.onreadystatechange = function () {
            if (xhr.readyState === XMLHttpRequest.DONE && xhr.status === 200) {
                try {
                    var parsed = JSON.parse(xhr.responseText);
                    if (parsed && parsed.revision !== serverRevision) {
                        applyServerState(parsed);
                    }
                } catch (e) {}
            }
        };
        xhr.send();
    }

    onPlayingChanged: {
        if (playing) {
            visible = true;
            updateClock();
            if (!slides || slides.length === 0) {
                if (fallbackSlides && fallbackSlides.length > 0) {
                    slides = shuffledCopy(fallbackSlides);
                    currentIndex = 0;
                    renderCurrent();
                }
            } else {
                renderCurrent();
            }
            pollServerState();
            slideTimer.restart();
        } else {
            slideTimer.stop();
            syncTimer.stop();
            clockTimer.stop();
            slotA.source = "";
            slotB.source = "";
            visible = false;
        }
    }
}
