import SwiftUI

@main
struct TimeTrackerMenuBarApp: App {
    @StateObject private var viewModel = TimerViewModel()

    var body: some Scene {
        MenuBarExtra("Work Timer", systemImage: viewModel.isRunning ? "timer.circle.fill" : "timer.circle") {
            MenuBarContentView(viewModel: viewModel)
                .frame(minWidth: 280)
                .padding()
        }
        .menuBarExtraStyle(.window)
    }
}
