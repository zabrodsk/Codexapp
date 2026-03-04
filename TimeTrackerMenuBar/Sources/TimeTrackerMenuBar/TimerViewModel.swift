import Foundation

final class TimerViewModel: ObservableObject {
    @Published private(set) var elapsedSeconds: TimeInterval = 0
    @Published private(set) var isRunning = false

    private var timer: Timer?
    private var startTime: Date?
    private var accumulatedSeconds: TimeInterval = 0

    var elapsedText: String {
        let total = Int(elapsedSeconds)
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        let seconds = total % 60
        return String(format: "%02d:%02d:%02d", hours, minutes, seconds)
    }

    func start() {
        guard !isRunning else { return }

        isRunning = true
        startTime = Date()

        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            self?.refreshElapsedTime()
        }
        timer?.tolerance = 0.15
        refreshElapsedTime()
    }

    func stop() {
        guard isRunning else { return }
        refreshElapsedTime()
        accumulatedSeconds = elapsedSeconds
        startTime = nil
        isRunning = false
        timer?.invalidate()
        timer = nil
    }

    func reset() {
        stop()
        accumulatedSeconds = 0
        elapsedSeconds = 0
    }

    deinit {
        timer?.invalidate()
    }

    private func refreshElapsedTime() {
        guard let startTime else {
            elapsedSeconds = accumulatedSeconds
            return
        }

        elapsedSeconds = accumulatedSeconds + Date().timeIntervalSince(startTime)
    }
}
