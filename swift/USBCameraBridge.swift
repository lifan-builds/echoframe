// Read-only macOS ImageCaptureCore iPhone USB catalog, 640px thumbnail, and 2048px original bridge.
// Contains zero delete calls — strictly non-destructive access to a USB-connected iPhone.
//
// Build:
//   swiftc -O swift/USBCameraBridge.swift -o data/bin/USBCameraBridge
// Run:
//   ./data/bin/USBCameraBridge ./data/usb_daemon

import Foundation
import ImageCaptureCore
import ImageIO
import UniformTypeIdentifiers

final class USBCameraDaemon: NSObject, ICDeviceBrowserDelegate, ICCameraDeviceDelegate {
    let baseDir: URL
    let thumbsDir: URL
    let requestsDir: URL
    let responsesDir: URL
    let stateFile: URL
    let catalogFile: URL

    let browser = ICDeviceBrowser()
    var camera: ICCameraDevice?
    var fileMap: [String: ICCameraFile] = [:]
    var orderedFiles: [(String, ICCameraFile)] = []

    var status: String = "searching_usb"
    var lastError: String = ""
    var catalogComplete = false
    var thumbsDone = 0
    var thumbsFailed = 0
    var thumbsRunning = false
    var thumbQueue: [(String, ICCameraFile)] = []
    var thumbNextIdx = 0
    var thumbActive = 0
    var processingRequest = false
    let isoFmt: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    init(baseDirPath: String) {
        self.baseDir = URL(fileURLWithPath: baseDirPath, isDirectory: true)
        self.thumbsDir = baseDir.appendingPathComponent("thumbs", isDirectory: true)
        self.requestsDir = baseDir.appendingPathComponent("requests", isDirectory: true)
        self.responsesDir = baseDir.appendingPathComponent("responses", isDirectory: true)
        self.stateFile = baseDir.appendingPathComponent("state.json")
        self.catalogFile = baseDir.appendingPathComponent("catalog.jsonl")
        super.init()

        let fm = FileManager.default
        try? fm.createDirectory(at: thumbsDir, withIntermediateDirectories: true)
        try? fm.createDirectory(at: requestsDir, withIntermediateDirectories: true)
        try? fm.createDirectory(at: responsesDir, withIntermediateDirectories: true)
    }

    func start() {
        writeState()
        browser.delegate = self
        browser.browsedDeviceTypeMask = ICDeviceTypeMask(
            rawValue: ICDeviceTypeMask.camera.rawValue | ICDeviceLocationTypeMask.local.rawValue
        )!
        browser.start()

        Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.tick()
        }
    }

    func sanitize(_ s: String) -> String {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "._-"))
        return String(s.unicodeScalars.map { allowed.contains($0) ? Character($0) : "_" })
    }

    func makeUSBID(for file: ICCameraFile, index: Int) -> String {
        let folder = sanitize(file.parentFolder?.name ?? "root")
        let name = sanitize(file.name ?? "item_\(index)")
        return "\(folder)__\(name)"
    }

    func writeState() {
        let dict: [String: Any] = [
            "updated_at": isoFmt.string(from: Date()),
            "status": status,
            "device_name": camera?.name ?? "",
            "icloud_photos": camera?.iCloudPhotosEnabled ?? false,
            "access_restricted": camera?.isAccessRestrictedAppleDevice ?? true,
            "catalog_pct": camera?.contentCatalogPercentCompleted ?? 0,
            "media_count": orderedFiles.count > 0 ? orderedFiles.count : (camera?.mediaFiles?.count ?? 0),
            "catalog_complete": catalogComplete,
            "thumbs_done": thumbsDone,
            "thumbs_failed": thumbsFailed,
            "last_error": lastError
        ]
        if let data = try? JSONSerialization.data(withJSONObject: dict, options: [.prettyPrinted, .sortedKeys]) {
            let tmp = baseDir.appendingPathComponent("state.json.tmp")
            try? data.write(to: tmp, options: .atomic)
            try? FileManager.default.removeItem(at: stateFile)
            try? FileManager.default.moveItem(at: tmp, to: stateFile)
        }
    }

    func tick() {
        if let cam = camera, !cam.hasOpenSession && !cam.isAccessRestrictedAppleDevice && status == "waiting_for_unlock" {
            status = "opening_session"
            cam.requestOpenSession()
        }
        writeState()
        if catalogComplete && !processingRequest {
            pollDownloadRequests()
        }
    }

    func deviceBrowser(_ browser: ICDeviceBrowser, didAdd device: ICDevice, moreComing: Bool) {
        guard let cam = device as? ICCameraDevice else { return }
        self.camera = cam
        cam.delegate = self
        status = cam.isAccessRestrictedAppleDevice ? "waiting_for_unlock" : "opening_session"
        writeState()
        cam.requestOpenSession()
    }

    func deviceBrowser(_ browser: ICDeviceBrowser, didRemove device: ICDevice, moreGoing: Bool) {
        if device == camera {
            status = "disconnected"
            catalogComplete = false
            camera = nil
            writeState()
        }
    }

    func device(_ device: ICDevice, didOpenSessionWithError error: (any Error)?) {
        if let err = error as NSError? {
            lastError = err.localizedDescription
            status = (err.code == -9943 || (camera?.isAccessRestrictedAppleDevice ?? false)) ? "waiting_for_unlock" : "session_error"
            writeState()
            return
        }
        lastError = ""
        status = "cataloging"
        writeState()
    }

    func cameraDeviceDidRemoveAccessRestriction(_ device: ICDevice) {
        lastError = ""
        status = "opening_session"
        writeState()
        camera?.requestOpenSession()
    }

    func cameraDeviceDidEnableAccessRestriction(_ device: ICDevice) {
        if !catalogComplete {
            status = "waiting_for_unlock"
            writeState()
        }
    }

    func deviceDidBecomeReady(withCompleteContentCatalog device: ICCameraDevice) {
        let rawFiles = (device.mediaFiles ?? []).compactMap { $0 as? ICCameraFile }
        var seen: [String: Int] = [:]
        fileMap.removeAll(keepingCapacity: true)
        orderedFiles.removeAll(keepingCapacity: true)

        var lines: [String] = []
        lines.reserveCapacity(rawFiles.count)

        for (idx, f) in rawFiles.enumerated() {
            var uid = makeUSBID(for: f, index: idx)
            if let c = seen[uid] {
                seen[uid] = c + 1
                uid = "\(uid)__\(c + 1)"
            } else {
                seen[uid] = 1
            }
            fileMap[uid] = f
            orderedFiles.append((uid, f))

            let name = f.name ?? ""
            let ext = (name as NSString).pathExtension.lowercased()
            let row: [String: Any] = [
                "usb_id": uid,
                "name": name,
                "folder": f.parentFolder?.name ?? "",
                "ext": ext,
                "uti": f.uti ?? "",
                "file_size": Int64(f.fileSize),
                "width": f.width,
                "height": f.height,
                "duration": f.duration,
                "orientation": Int(f.orientation.rawValue),
                "creation_date": f.creationDate != nil ? isoFmt.string(from: f.creationDate!) : "",
                "modification_date": f.modificationDate != nil ? isoFmt.string(from: f.modificationDate!) : "",
                "burst_uuid": f.burstUUID ?? "",
                "burst_picked": f.burstPicked,
                "burst_favorite": f.burstFavorite,
                "related_uuid": f.relatedUUID ?? "",
                "group_uuid": f.groupUUID ?? "",
                "created_filename": f.createdFilename ?? "",
                "original_filename": f.originalFilename ?? "",
                "high_framerate": f.highFramerate,
                "time_lapse": f.timeLapse
            ]
            if let d = try? JSONSerialization.data(withJSONObject: row),
               let s = String(data: d, encoding: .utf8) {
                lines.append(s)
            }
        }

        if !lines.isEmpty {
            let joined = lines.joined(separator: "\n") + "\n"
            try? joined.write(to: catalogFile, atomically: true, encoding: .utf8)
        }
        catalogComplete = true
        status = "syncing_thumbs"
        writeState()
        startThumbnailSync()
    }

    func normalizeToJPEG(_ rawData: Data) -> Data? {
        guard let src = CGImageSourceCreateWithData(rawData as CFData, nil) else { return nil }
        let opts: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: 640
        ]
        guard let cgImg = CGImageSourceCreateThumbnailAtIndex(src, 0, opts as CFDictionary) else { return rawData }
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(out, UTType.jpeg.identifier as CFString, 1, nil) else { return rawData }
        let destOpts: [CFString: Any] = [kCGImageDestinationLossyCompressionQuality: 0.84]
        CGImageDestinationAddImage(dest, cgImg, destOpts as CFDictionary)
        return CGImageDestinationFinalize(dest) ? (out as Data) : rawData
    }

    func startThumbnailSync() {
        guard !thumbsRunning else { return }
        thumbsRunning = true
        let fm = FileManager.default
        let supportedExts: Set<String> = ["heic", "heif", "jpg", "jpeg", "png", "webp", "tif", "tiff", "dng", "mov", "mp4", "m4v"]

        var queue: [(String, ICCameraFile)] = []
        var preExisting = 0
        for (uid, f) in orderedFiles {
            let ext = ((f.name ?? "") as NSString).pathExtension.lowercased()
            if !supportedExts.contains(ext) { continue }
            let outURL = thumbsDir.appendingPathComponent("\(uid).jpg")
            if fm.fileExists(atPath: outURL.path) {
                preExisting += 1
            } else {
                queue.append((uid, f))
            }
        }
        thumbQueue = queue
        thumbNextIdx = 0
        thumbActive = 0
        thumbsDone = preExisting
        writeState()
        pumpThumbnails()
    }

    func pumpThumbnails() {
        let maxConcurrency = 6
        while thumbActive < maxConcurrency && thumbNextIdx < thumbQueue.count {
            let (uid, file) = thumbQueue[thumbNextIdx]
            thumbNextIdx += 1
            thumbActive += 1
            let outURL = thumbsDir.appendingPathComponent("\(uid).jpg")
            let opts: [ICCameraItemThumbnailOption: Any] = [.imageSourceThumbnailMaxPixelSize: 640]
            file.requestThumbnailData(options: opts) { [weak self] data, _ in
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    self.thumbActive -= 1
                    if let raw = data, let jpg = self.normalizeToJPEG(raw) {
                        try? jpg.write(to: outURL, options: .atomic)
                        self.thumbsDone += 1
                    } else {
                        self.thumbsFailed += 1
                    }
                    if (self.thumbsDone + self.thumbsFailed) % 50 == 0 {
                        self.writeState()
                    }
                    if self.thumbNextIdx >= self.thumbQueue.count && self.thumbActive == 0 {
                        self.thumbsRunning = false
                        self.status = "ready"
                        self.writeState()
                    } else {
                        self.pumpThumbnails()
                    }
                }
            }
        }
        if thumbQueue.isEmpty && thumbActive == 0 {
            thumbsRunning = false
            status = "ready"
            writeState()
        }
    }

    func pollDownloadRequests() {
        let fm = FileManager.default
        guard let entries = try? fm.contentsOfDirectory(at: requestsDir, includingPropertiesForKeys: nil) else { return }
        let jsonFiles = entries.filter { $0.pathExtension == "json" }.sorted { $0.lastPathComponent < $1.lastPathComponent }
        guard let reqFile = jsonFiles.first else { return }

        guard let raw = try? Data(contentsOf: reqFile),
              let obj = try? JSONSerialization.jsonObject(with: raw) as? [String: Any],
              let reqID = obj["request_id"] as? String,
              let usbIDs = obj["usb_ids"] as? [String],
              let outDirStr = obj["out_dir"] as? String else {
            try? fm.removeItem(at: reqFile)
            return
        }
        try? fm.removeItem(at: reqFile)
        processingRequest = true

        let outDir = URL(fileURLWithPath: outDirStr, isDirectory: true)
        try? fm.createDirectory(at: outDir, withIntermediateDirectories: true)

        var downloaded: [String: String] = [:]
        var errors: [String: String] = [:]
        var idx = 0

        func downloadNext() {
            if idx >= usbIDs.count {
                let resp: [String: Any] = [
                    "request_id": reqID,
                    "downloaded": downloaded,
                    "errors": errors,
                    "completed_at": isoFmt.string(from: Date())
                ]
                if let respData = try? JSONSerialization.data(withJSONObject: resp, options: [.prettyPrinted]) {
                    try? respData.write(to: responsesDir.appendingPathComponent("\(reqID).json"), options: .atomic)
                }
                processingRequest = false
                return
            }
            let uid = usbIDs[idx]
            idx += 1
            guard let file = fileMap[uid] else {
                errors[uid] = "not_found_in_catalog"
                downloadNext()
                return
            }
            let ext = ((file.name ?? "") as NSString).pathExtension
            let saveName = ext.isEmpty ? uid : (uid.lowercased().hasSuffix(".\(ext.lowercased())") ? uid : "\(uid).\(ext)")
            let targetPath = outDir.appendingPathComponent(saveName)
            if fm.fileExists(atPath: targetPath.path) {
                downloaded[uid] = targetPath.path
                downloadNext()
                return
            }
            let opts: [ICDownloadOption: Any] = [
                .downloadsDirectoryURL: outDir,
                .saveAsFilename: saveName,
                .overwrite: true
            ]
            _ = file.requestDownload(options: opts) { savedFilename, err in
                DispatchQueue.main.async {
                    if let err = err {
                        errors[uid] = err.localizedDescription
                    } else {
                        let actualName = savedFilename ?? saveName
                        downloaded[uid] = outDir.appendingPathComponent(actualName).path
                    }
                    downloadNext()
                }
            }
        }
        downloadNext()
    }

    func device(_ device: ICDevice, didCloseSessionWithError error: (any Error)?) {}
    func didRemove(_ device: ICDevice) {}
    func cameraDevice(_ camera: ICCameraDevice, didAdd items: [ICCameraItem]) {}
    func cameraDevice(_ camera: ICCameraDevice, didRemove items: [ICCameraItem]) {}
    func cameraDevice(_ camera: ICCameraDevice, didRenameItems items: [ICCameraItem]) {}
    func cameraDevice(_ camera: ICCameraDevice, didCompleteDeleteFilesWithError error: (any Error)?) {}
    func cameraDeviceDidChangeCapability(_ camera: ICCameraDevice) {}
    func cameraDevice(_ camera: ICCameraDevice, didReceiveThumbnail thumbnail: CGImage?, for item: ICCameraItem, error: (any Error)?) {}
    func cameraDevice(_ camera: ICCameraDevice, didReceiveMetadata metadata: [AnyHashable : Any]?, for item: ICCameraItem, error: (any Error)?) {}
    func cameraDevice(_ camera: ICCameraDevice, didReceivePTPEvent eventData: Data) {}
}

let basePath = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : (FileManager.default.currentDirectoryPath + "/data/usb_daemon")

let daemon = USBCameraDaemon(baseDirPath: basePath)
daemon.start()
RunLoop.main.run()
