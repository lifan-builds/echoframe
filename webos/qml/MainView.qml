import QtQuick 2.6
import Eos.Items 0.1

FocusScope {
    id: root

    property string fgAppId: ""
    property bool hasPigScreen: false
    property bool hasVideo: false
    property bool partial: false
    property int mode: 1
    property alias screenSaver: loader.item
    property bool enablePigScreenSaver: true
    property bool oledScreen: false
    property bool initialized: false
    property string guideString: ""
    property bool guideCompleted: true
    property bool startAdScreenSaver: false

    focus: true

    width: parent.width
    height: parent.height
    anchors.top: parent.top
    anchors.horizontalCenter: parent.horizontalCenter

    Loader {
        id: loader
        anchors.fill: parent
        focus: true

        onLoaded: {
            printLog("[EchoFrame] ScreenSaver loaded");
            if (item) {
                item.playing = true;
            }
        }
    }

    Component {
        id: mode1

        ScreenSaver {
            anchors.fill: parent
            liteMode: (root.width === 1280)
            playing: false
            visible: true
            isPartial: root.partial
            guideString: ""
        }
    }

    onStateChanged: {
        printLog("[EchoFrame] MainView state: " + state);
        switch (state) {
        case "Mode_ScreenSaver":
            pigMasker.visible = false;
            rootWindow.color = "black";
            loader.sourceComponent = mode1;
            break;
        case "Mode_PIG":
            pigMasker.visible = true;
            rootWindow.color = "black";
            loader.sourceComponent = undefined;
            break;
        case "Mode_None":
            pigMasker.visible = false;
            rootWindow.color = "transparent";
            loader.sourceComponent = undefined;
            break;
        }
    }

    state: checkPIGState(root.initialized, systemProperties.screenStatus, systemProperties.foregroundAppInfo, systemProperties.tvInfo)

    states: [
        State { name: "Mode_None" },
        State { name: "Mode_ScreenSaver" },
        State { name: "Mode_PIG" }
    ]

    function startTimer() {}

    function isValidPIGScreen(positionX, positionY, width, height) {
        if (positionX >= root.width || positionY >= root.height) return false;
        if (width < 1 || height < 1) return false;
        return true;
    }

    function getRelativeWidth(width) {
        return Math.round(width * (root.width / 1920));
    }

    function getRelativeHeight(height) {
        return Math.round(height * (root.height / 1080));
    }

    PunchThrough {
        id: pigMasker
        visible: false
    }

    function initialize() {
        interfaces.application.getAppInfo(systemProperties.foreGroundAppId, function(response) {
            if (response && response.appInfo && response.appInfo.hasOwnProperty('enablePigScreenSaver')) {
                enablePigScreenSaver = response.appInfo.enablePigScreenSaver;
            } else {
                enablePigScreenSaver = true;
            }
            root.initialized = true;
        });
    }

    function checkPIGState(initialized, screenStatus, mediaInfo, tvInfo) {
        if (!initialized) {
            return "Mode_None";
        }

        var tempX, tempY, tempWidth, tempHeight;
        var displayOutput;
        var isFullScreen;
        var context = "";
        var mediaIndex = -1;
        var tvIndex = -1;
        var windowId = "";

        if (screenStatus && screenStatus.foregroundVideoWindow && screenStatus.foregroundVideoWindow[0] !== undefined
                && screenStatus.foregroundVideoWindow[0].hasOwnProperty('displayOutput')
                && screenStatus.foregroundVideoWindow[0].hasOwnProperty('fullScreen')) {
            displayOutput = screenStatus.foregroundVideoWindow[0].displayOutput;
            isFullScreen = screenStatus.foregroundVideoWindow[0].fullScreen;

            tempX = displayOutput.x * root.width / rootWindow.screenWidth;
            tempY = displayOutput.y * root.height / rootWindow.screenHeight;
            tempWidth = displayOutput.width * root.width / rootWindow.screenWidth;
            tempHeight = displayOutput.height * root.height / rootWindow.screenHeight;

            pigMasker.x = Math.max(tempX, 0);
            pigMasker.y = Math.max(tempY, 0);
            pigMasker.width = Math.min(tempWidth, tempX + tempWidth);
            pigMasker.height = Math.min(tempHeight, tempY + tempHeight);

            hasPigScreen = isFullScreen ? false : isValidPIGScreen(pigMasker.x, pigMasker.y, pigMasker.width, pigMasker.height);
            context = screenStatus.foregroundVideoWindow[0].context || "";
            windowId = screenStatus.foregroundVideoWindow[0].windowId || "";
        } else {
            hasPigScreen = false;
        }

        if (windowId !== "" && tvInfo && Array.isArray(tvInfo) && tvInfo.length > 0) {
            tvIndex = tvInfo.findIndex(function (item) {
                return item.windowId === windowId && item.windowStatus === "unblocked";
            });
        }
        systemProperties.isTvPlaying = tvIndex >= 0;

        if (context !== "" && mediaInfo && mediaInfo.foregroundAppInfo
                && Array.isArray(mediaInfo.foregroundAppInfo) && mediaInfo.foregroundAppInfo.length > 0) {
            mediaIndex = mediaInfo.foregroundAppInfo.findIndex(function (item) {
                return item.mediaId === context && item.playState === "playing";
            });
        }
        systemProperties.isMediaPlaying = mediaIndex >= 0;

        if (enablePigScreenSaver && hasPigScreen) {
            if (systemProperties.isTvPlaying || systemProperties.isExtInputPlaying || systemProperties.isMediaPlaying) {
                return "Mode_PIG";
            }
        }
        return "Mode_ScreenSaver";
    }

    function resetGuide() {}
}
