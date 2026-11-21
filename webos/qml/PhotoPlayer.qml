import QtQuick 2.6
import WebOSServices 1.0
import "../../View"

TvComponentBase {
    id: photoPlayerRoot
    componentId: "photoPlayer"
    needActiveFocus: false
    containToIdleStatus: true
    anchors.fill: parent
    color: "#080a0f"
    z: -9

    property var photoModel: modelManager.getModel("noSignalExPhotoModel")
    property int currentIdx: photoModel ? photoModel.currentIdx : 0
    property var imgArray: photoModel ? photoModel.imgArray : []
    property bool showingA: true

    visible: photoModel ? photoModel.enabled : false

    function currentUrl() {
        if (!imgArray || !imgArray.length) return "";
        var path = imgArray[currentIdx % imgArray.length];
        if (!path) return "";
        return path.indexOf("file://") === 0 ? path : ("file://" + path);
    }

    function showCurrent() {
        if (!visible) return;
        var nextUrl = currentUrl();
        if (!nextUrl) return;
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

    onVisibleChanged: {
        if (visible) {
            showCurrent();
        } else {
            slotA.opacity = 0;
            slotB.opacity = 0;
            slotA.source = "";
            slotB.source = "";
        }
    }

    onCurrentIdxChanged: {
        if (visible) {
            showCurrent();
        }
    }

    onImgArrayChanged: {
        if (visible) {
            showCurrent();
        }
    }

    Image {
        id: slotA
        anchors.fill: parent
        asynchronous: true
        cache: false
        smooth: true
        fillMode: Image.PreserveAspectFit
        sourceSize.width: parent.width > 0 ? parent.width : 1920
        sourceSize.height: parent.height > 0 ? parent.height : 1080
        opacity: 0
        z: photoPlayerRoot.showingA ? 2 : 1

        Behavior on opacity {
            NumberAnimation {
                duration: 850
                easing.type: Easing.OutCubic
                onRunningChanged: {
                    if (!running && slotA.opacity === 0 && !photoPlayerRoot.showingA) {
                        slotA.source = "";
                    }
                }
            }
        }

        onStatusChanged: {
            if (status === Image.Ready && photoPlayerRoot.showingA) {
                slotA.opacity = 1;
                slotB.opacity = 0;
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
        sourceSize.width: parent.width > 0 ? parent.width : 1920
        sourceSize.height: parent.height > 0 ? parent.height : 1080
        opacity: 0
        z: photoPlayerRoot.showingA ? 1 : 2

        Behavior on opacity {
            NumberAnimation {
                duration: 850
                easing.type: Easing.OutCubic
                onRunningChanged: {
                    if (!running && slotB.opacity === 0 && photoPlayerRoot.showingA) {
                        slotB.source = "";
                    }
                }
            }
        }

        onStatusChanged: {
            if (status === Image.Ready && !photoPlayerRoot.showingA) {
                slotB.opacity = 1;
                slotA.opacity = 0;
            }
        }
    }

    function hide(event) {
        console.log("[PhotoPlayer] cannot hide.");
    }
}
