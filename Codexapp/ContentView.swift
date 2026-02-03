import SwiftUI

struct ContentView: View {
    var body: some View {
        ZStack {
            LinearGradient(
                colors: [
                    Color(red: 0.16, green: 0.2, blue: 0.33),
                    Color(red: 0.06, green: 0.08, blue: 0.16)
                ],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
            .ignoresSafeArea()

            FloatingOrbs()
                .opacity(0.35)
                .ignoresSafeArea()

            VStack(spacing: 24) {
                HeaderView()

                GameStageView()
                    .padding(.horizontal, 20)

                ControlsView()
            }
            .padding(.top, 24)
        }
    }
}

private struct HeaderView: View {
    var body: some View {
        VStack(spacing: 8) {
            Text("Glassy Dash")
                .font(.system(size: 34, weight: .bold, design: .rounded))
                .foregroundStyle(.white)

            Text("Tap the luminous shard before it fades.")
                .font(.system(size: 16, weight: .medium, design: .rounded))
                .foregroundStyle(.white.opacity(0.75))
        }
        .multilineTextAlignment(.center)
        .padding(.horizontal, 24)
    }
}

private struct GameStageView: View {
    @State private var score = 0
    @State private var streak = 0
    @State private var timeRemaining = 30
    @State private var targetPosition: CGPoint = .init(x: 150, y: 200)
    @State private var targetScale: CGFloat = 1
    @State private var isPlaying = false

    private let timer = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    var body: some View {
        GlassCard {
            GeometryReader { proxy in
                ZStack {
                    VStack(spacing: 12) {
                        HStack {
                            StatChip(label: "Score", value: "\(score)")
                            Spacer()
                            StatChip(label: "Time", value: "\(timeRemaining)s")
                        }

                        HStack {
                            StatChip(label: "Streak", value: "\(streak)")
                            Spacer()
                        }
                    }
                    .padding(20)
                    .frame(maxWidth: .infinity, alignment: .topLeading)

                    Button(action: {
                        handleHit(in: proxy.size)
                    }) {
                        ZStack {
                            Circle()
                                .fill(Color.white.opacity(0.45))
                                .blur(radius: 2)

                            Circle()
                                .strokeBorder(
                                    LinearGradient(
                                        colors: [Color.white.opacity(0.9), Color.white.opacity(0.1)],
                                        startPoint: .topLeading,
                                        endPoint: .bottomTrailing
                                    ),
                                    lineWidth: 2
                                )

                            Image(systemName: "sparkle")
                                .font(.system(size: 26, weight: .bold))
                                .foregroundStyle(.white)
                        }
                        .frame(width: 76, height: 76)
                        .scaleEffect(targetScale)
                        .shadow(color: Color.white.opacity(0.35), radius: 16, x: 0, y: 8)
                    }
                    .position(targetPosition)
                    .disabled(!isPlaying)
                    .onAppear {
                        resetTarget(in: proxy.size)
                    }
                }
                .onReceive(timer) { _ in
                    guard isPlaying else { return }
                    if timeRemaining > 0 {
                        timeRemaining -= 1
                        pulseTarget()
                    } else {
                        endGame()
                    }
                }
                .onReceive(NotificationCenter.default.publisher(for: .gamePlayToggled)) { notification in
                    if let isPlaying = notification.userInfo?["isPlaying"] as? Bool {
                        if isPlaying {
                            startGame(in: proxy.size)
                        } else {
                            endGame()
                        }
                    }
                }
            }
            .frame(height: 420)
        }
    }

    private func handleHit(in size: CGSize) {
        guard isPlaying else { return }
        score += 10
        streak += 1
        resetTarget(in: size)
    }

    private func resetTarget(in size: CGSize) {
        let padding: CGFloat = 60
        let x = CGFloat.random(in: padding...(size.width - padding))
        let y = CGFloat.random(in: 120...(size.height - padding))
        withAnimation(.spring(response: 0.6, dampingFraction: 0.7)) {
            targetPosition = CGPoint(x: x, y: y)
            targetScale = 1
        }
    }

    private func pulseTarget() {
        withAnimation(.easeInOut(duration: 0.9)) {
            targetScale = targetScale == 1 ? 0.85 : 1
        }
    }

    private func startGame(in size: CGSize) {
        score = 0
        streak = 0
        timeRemaining = 30
        isPlaying = true
        resetTarget(in: size)
        NotificationCenter.default.post(
            name: .gamePlayStateUpdated,
            object: nil,
            userInfo: ["isPlaying": true]
        )
    }

    private func endGame() {
        isPlaying = false
        streak = 0
        withAnimation(.easeOut(duration: 0.4)) {
            targetScale = 0.9
        }
        NotificationCenter.default.post(
            name: .gamePlayStateUpdated,
            object: nil,
            userInfo: ["isPlaying": false]
        )
    }
}

private struct ControlsView: View {
    @State private var isPlaying = false

    var body: some View {
        GlassCard {
            VStack(spacing: 16) {
                Text(isPlaying ? "Keep your focus!" : "Ready to play?")
                    .font(.system(size: 18, weight: .semibold, design: .rounded))
                    .foregroundStyle(.white)

                Button {
                    isPlaying.toggle()
                    NotificationCenter.default.post(
                        name: .gamePlayToggled,
                        object: nil,
                        userInfo: ["isPlaying": isPlaying]
                    )
                } label: {
                    Text(isPlaying ? "End Session" : "Start Session")
                        .font(.system(size: 18, weight: .bold, design: .rounded))
                        .padding(.vertical, 14)
                        .frame(maxWidth: .infinity)
                        .background(
                            LinearGradient(
                                colors: [Color.white.opacity(0.7), Color.white.opacity(0.3)],
                                startPoint: .topLeading,
                                endPoint: .bottomTrailing
                            )
                        )
                        .foregroundStyle(Color(red: 0.11, green: 0.14, blue: 0.24))
                        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
                }
            }
            .padding(20)
        }
        .padding(.horizontal, 20)
        .onReceive(NotificationCenter.default.publisher(for: .gamePlayStateUpdated)) { notification in
            if let isPlaying = notification.userInfo?["isPlaying"] as? Bool {
                self.isPlaying = isPlaying
            }
        }
    }
}

private struct GlassCard<Content: View>: View {
    let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        content
            .background(
                RoundedRectangle(cornerRadius: 28, style: .continuous)
                    .fill(.ultraThinMaterial)
                    .overlay(
                        RoundedRectangle(cornerRadius: 28, style: .continuous)
                            .stroke(Color.white.opacity(0.35), lineWidth: 1)
                    )
            )
            .shadow(color: Color.black.opacity(0.2), radius: 20, x: 0, y: 12)
    }
}

private struct StatChip: View {
    let label: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label.uppercased())
                .font(.system(size: 10, weight: .semibold, design: .rounded))
                .foregroundStyle(.white.opacity(0.6))

            Text(value)
                .font(.system(size: 18, weight: .bold, design: .rounded))
                .foregroundStyle(.white)
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 12)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(Color.white.opacity(0.16))
        )
    }
}

private struct FloatingOrbs: View {
    var body: some View {
        ZStack {
            Circle()
                .fill(Color(red: 0.38, green: 0.62, blue: 0.95))
                .frame(width: 240, height: 240)
                .blur(radius: 30)
                .offset(x: -140, y: -240)

            Circle()
                .fill(Color(red: 0.64, green: 0.42, blue: 0.95))
                .frame(width: 220, height: 220)
                .blur(radius: 30)
                .offset(x: 160, y: -140)

            Circle()
                .fill(Color(red: 0.35, green: 0.88, blue: 0.82))
                .frame(width: 260, height: 260)
                .blur(radius: 40)
                .offset(x: 0, y: 240)
        }
    }
}

extension Notification.Name {
    static let gamePlayToggled = Notification.Name("gamePlayToggled")
    static let gamePlayStateUpdated = Notification.Name("gamePlayStateUpdated")
}

#Preview {
    ContentView()
}
