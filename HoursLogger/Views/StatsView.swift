import SwiftUI

struct StatsView: View {
    @EnvironmentObject private var store: WorkStore

    var body: some View {
        NavigationStack {
            List {
                Section("Totals") {
                    StatRow(title: "Week", value: hours(totalMinutes(for: .week)))
                    StatRow(title: "Month", value: hours(totalMinutes(for: .month)))
                    StatRow(title: "All Time", value: hours(store.entries.reduce(0) { $0 + $1.durationMinutes }))
                }

                Section("By Group") {
                    ForEach(groupTotals, id: \.group) { item in
                        StatRow(title: item.group, value: hours(item.minutes))
                    }
                }

                Section("Session Insights") {
                    StatRow(title: "Average Length", value: hours(averageSessionMinutes))
                    StatRow(title: "Count", value: "\(store.entries.count)")
                }
            }
            .navigationTitle("Stats")
        }
    }

    private func totalMinutes(for component: Calendar.Component) -> Int {
        let calendar = Calendar.current
        let now = Date()
        return store.entries
            .filter { calendar.isDate($0.date, equalTo: now, toGranularity: component) }
            .reduce(0) { $0 + $1.durationMinutes }
    }

    private var averageSessionMinutes: Int {
        guard !store.entries.isEmpty else { return 0 }
        return store.entries.reduce(0) { $0 + $1.durationMinutes } / store.entries.count
    }

    private var groupTotals: [(group: String, minutes: Int)] {
        let grouped = Dictionary(grouping: store.entries) { $0.groupLabel }
        return grouped.map { group, entries in
            (group, entries.reduce(0) { $0 + $1.durationMinutes })
        }
        .sorted { $0.minutes > $1.minutes }
    }

    private func hours(_ minutes: Int) -> String {
        String(format: "%.2f h", Double(minutes) / 60.0)
    }
}

private struct StatRow: View {
    let title: String
    let value: String

    var body: some View {
        HStack {
            Text(title)
            Spacer()
            Text(value)
                .foregroundStyle(.secondary)
        }
    }
}

#Preview {
    StatsView()
        .environmentObject(WorkStore())
}
