import SwiftUI

struct MenuBarContentView: View {
    @ObservedObject var viewModel: TimerViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Work Time")
                .font(.headline)

            Text(viewModel.elapsedText)
                .font(.system(size: 34, weight: .semibold, design: .monospaced))
                .frame(maxWidth: .infinity, alignment: .center)

            HStack(spacing: 10) {
                Button(viewModel.isRunning ? "Stop" : "Start") {
                    if viewModel.isRunning {
                        viewModel.stop()
                    } else {
                        viewModel.start()
                    }
                }
                .keyboardShortcut(.space, modifiers: [])
                .buttonStyle(.borderedProminent)

                Button("Reset") {
                    viewModel.reset()
                }
                .buttonStyle(.bordered)
                .disabled(viewModel.isRunning && viewModel.elapsedSeconds < 1)
            }

            Divider()

            Text(viewModel.isRunning ? "Currently tracking your work session." : "Timer is paused. Start when work begins.")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
