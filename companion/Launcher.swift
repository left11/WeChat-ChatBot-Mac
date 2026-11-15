import AppKit
import WebKit
import Darwin

// The launcher owns only this Process. It never discovers or signals WeChat.
final class WorkBridge: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var statusItem: NSStatusItem!
    private var service: Process?
    private var stdoutPipe: Pipe?
    private var stderrPipe: Pipe?
    private var startupBuffer = Data()
    private var stderrBuffer = Data()
    private var localPort: Int?
    private var quitting = false
    private var failed = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenus()
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.userContentController.add(self, name: "clipboard")
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "微信工作桥"
        window.minSize = NSSize(width: 980, height: 650)
        window.contentView = webView
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.center()
        openWindow()
        startService()
    }

    private func buildMenus() {
        let main = NSMenu()
        let app = NSMenu()
        app.addItem(withTitle: "关于微信工作桥", action: #selector(showAbout), keyEquivalent: "")
        app.addItem(.separator())
        app.addItem(withTitle: "隐藏微信工作桥", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        app.addItem(.separator())
        app.addItem(withTitle: "退出微信工作桥", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let appItem = NSMenuItem(); appItem.submenu = app; main.addItem(appItem)
        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        let editItem = NSMenuItem(title: "编辑", action: nil, keyEquivalent: ""); editItem.submenu = edit; main.addItem(editItem)
        NSApp.mainMenu = main
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "tray.full", accessibilityDescription: "微信工作桥")
        let statusMenu = NSMenu()
        statusMenu.addItem(withTitle: "打开微信工作桥", action: #selector(openWindow), keyEquivalent: "")
        statusMenu.addItem(withTitle: "连接 WorkBuddy", action: #selector(openConnections), keyEquivalent: "")
        statusMenu.addItem(.separator())
        statusMenu.addItem(withTitle: "退出微信工作桥", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        for item in statusMenu.items where item.action != #selector(NSApplication.terminate(_:)) { item.target = self }
        statusItem.menu = statusMenu
    }

    @objc private func showAbout() {
        NSApp.orderFrontStandardAboutPanel(options: [.applicationName: "微信工作桥", .applicationVersion: "0.1.0", .credits: NSAttributedString(string: "把消息接到工作里。\n聊天原文、工作便笺与回复草稿保存在这台 Mac。")])
    }

    @objc private func openWindow() {
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func openConnections() {
        openWindow()
        webView.evaluateJavaScript("window.workBridge && window.workBridge.openConnections()", completionHandler: nil)
    }

    private func startService() {
        guard let resources = Bundle.main.resourceURL else { showFailure("无法找到应用资源。"); return }
        let script = resources.appendingPathComponent("service.py")
        guard FileManager.default.fileExists(atPath: script.path) else { showFailure("本地服务文件缺失，请重新构建应用。"); return }
        guard FileManager.default.isExecutableFile(atPath: "/usr/bin/python3") else { showFailure("这台 Mac 尚未提供 Python 3。当前预览版需要先安装 Apple 命令行开发工具。"); return }
        let process = Process(), output = Pipe(), errors = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-u", script.path, "--serve", "--port", "0"]
        process.currentDirectoryURL = resources
        process.standardOutput = output
        process.standardError = errors
        process.standardInput = FileHandle.nullDevice
        service = process; stdoutPipe = output; stderrPipe = errors
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            DispatchQueue.main.async { self?.receiveStartup(data) }
        }
        errors.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            DispatchQueue.main.async {
                guard let self = self else { return }
                if self.stderrBuffer.count < 32768 { self.stderrBuffer.append(data.prefix(32768 - self.stderrBuffer.count)) }
            }
        }
        process.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                guard let self = self, !self.quitting, !self.failed else { return }
                self.showFailure("本地服务已停止（状态 \(process.terminationStatus)）。请退出后重新打开工作桥。")
            }
        }
        do { try process.run() }
        catch { showFailure("本地服务未能启动：\(error.localizedDescription)"); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 15) { [weak self] in
            guard let self = self, self.localPort == nil, !self.failed, !self.quitting else { return }
            self.showFailure("本地服务启动超时，请退出后重新打开工作桥。")
        }
    }

    private func receiveStartup(_ data: Data) {
        guard localPort == nil, !failed, !quitting else { return }
        startupBuffer.append(data)
        guard startupBuffer.count <= 65536 else { showFailure("本地服务返回了无效的启动信息。"); return }
        guard let newline = startupBuffer.firstIndex(of: 10) else { return }
        let line = startupBuffer.prefix(upTo: newline)
        guard let info = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
              let address = info["url"] as? String,
              let token = info["token"] as? String, !token.isEmpty,
              let pid = info["pid"] as? Int, pid == Int(service?.processIdentifier ?? -1),
              var components = URLComponents(string: address),
              components.scheme == "http", components.host == "127.0.0.1",
              components.user == nil, components.password == nil,
              let port = components.port, port > 0, port < 65536 else {
            showFailure("本地服务的身份或地址校验失败。"); return
        }
        components.path = "/"; components.query = nil
        var fragment = URLComponents(); fragment.queryItems = [URLQueryItem(name: "token", value: token)]
        components.percentEncodedFragment = fragment.percentEncodedQuery
        guard let url = components.url else { showFailure("本地服务地址无效。"); return }
        localPort = port; startupBuffer.removeAll()
        webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData))
    }

    private func showFailure(_ message: String) {
        guard !failed, !quitting else { return }; failed = true
        if let service = service, service.isRunning { service.terminate() }
        let alert = NSAlert(); alert.messageText = "工作桥暂时无法启动"; alert.informativeText = message
        alert.addButton(withTitle: "退出")
        alert.beginSheetModal(for: window) { _ in NSApp.terminate(nil) }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if url.scheme == "http", url.host == "127.0.0.1", url.port == localPort, url.user == nil, url.password == nil {
            decisionHandler(.allow); return
        }
        if navigationAction.navigationType == .linkActivated, ["https", "http"].contains(url.scheme ?? "") {
            NSWorkspace.shared.open(url)
        }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert(); alert.messageText = "确认当前操作"; alert.informativeText = message
        alert.addButton(withTitle: "继续"); alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "clipboard", message.frameInfo.isMainFrame,
              message.frameInfo.securityOrigin.protocol == "http",
              message.frameInfo.securityOrigin.host == "127.0.0.1",
              message.frameInfo.securityOrigin.port == localPort,
              let body = message.body as? [String: Any], let text = body["text"] as? String,
              text.utf8.count <= 1048576 else { return }
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(text, forType: .string)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool { sender.orderOut(nil); return false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { openWindow(); return true }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        quitting = true
        stdoutPipe?.fileHandleForReading.readabilityHandler = nil
        stderrPipe?.fileHandleForReading.readabilityHandler = nil
        guard let service = service, service.isRunning else { return .terminateNow }
        service.terminate()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
            // Process tracks this child with waitpid; no discovery or broad kill.
            if service.isRunning { Darwin.kill(service.processIdentifier, SIGKILL) }
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}

let app = NSApplication.shared
let delegate = WorkBridge()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
