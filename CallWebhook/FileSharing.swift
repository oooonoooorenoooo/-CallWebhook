import SwiftUI
import UIKit

struct SharedFiles: Identifiable {
    let id = UUID()
    let urls: [URL]
    private static var directory: URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("CallWebhookExports", isDirectory: true)
    }

    static func make(data: Data, name: String) throws -> SharedFiles {
        let folder = directory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let url = folder.appendingPathComponent(name)
        try data.write(to: url, options: .atomic)
        return SharedFiles(urls: [url])
    }

    static func cleanTemporaryFiles() {
        try? FileManager.default.removeItem(at: directory)
    }
}

struct FileShareSheet: UIViewControllerRepresentable {
    let urls: [URL]
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: urls, applicationActivities: nil)
    }
    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}

struct FileExportPicker: UIViewControllerRepresentable {
    let urls: [URL]
    func makeUIViewController(context: Context) -> UIDocumentPickerViewController {
        let picker = UIDocumentPickerViewController(forExporting: urls, asCopy: true)
        picker.shouldShowFileExtensions = true
        return picker
    }
    func updateUIViewController(_ controller: UIDocumentPickerViewController, context: Context) {}
}
