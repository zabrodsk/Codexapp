import SwiftUI

struct TerrainStatusCard: View {
    let state: TerrainState

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Dynamic Terrain")
                .font(.caption)
                .foregroundStyle(.secondary)
            Text("Snow: \(Int(state.snowDepthCM))cm")
            Text("Visibility: \(state.visibility.rawValue.capitalized)")
            Text("Groomed: \(Int(state.groomedRatio * 100))%")
        }
        .font(.footnote.weight(.semibold))
        .padding(10)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12))
    }
}

struct LiveHUDCard: View {
    let hud: HUDState
    let resort: Resort

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(hud.zoneName)
                .font(.headline)
            Text(hud.xpTick)
                .font(.subheadline)
            ProgressView(value: hud.festivalProgress) {
                Text("Festival Progress")
            }
            .tint(.green)
            Text("Vertical: \(hud.verticalMeters)m | x\(hud.comboMultiplier)")
                .font(.footnote)
            Text("\(resort.events.filter(\.isCompleted).count)/\(resort.events.count) events done")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(maxWidth: 230, alignment: .leading)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
    }
}

struct EventPreviewSheet: View {
    let event: MapEvent
    let completeAction: () -> Void

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 16) {
                Label(event.type.rawValue.capitalized, systemImage: "map")
                    .font(.headline)
                Text(event.title)
                    .font(.largeTitle.bold())
                Text("Difficulty: \(event.difficulty)/5")
                Text("XP Reward: \(event.reward.xp)")
                if let badge = event.reward.badge {
                    Text("Badge: \(badge)")
                }

                Button(event.isCompleted ? "Completed" : "Complete Event") {
                    completeAction()
                }
                .buttonStyle(.borderedProminent)
                .disabled(event.isCompleted)

                Spacer()
            }
            .padding()
            .navigationTitle("Event Preview")
        }
    }
}
