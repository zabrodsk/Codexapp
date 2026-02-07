import SwiftUI

struct TimelineView: View {
    @EnvironmentObject private var store: WorkStore
    @State private var selectedEntry: WorkEntry?

    var body: some View {
        NavigationStack {
            List {
                Section {
                    WeeklyMonthlySummary(entries: store.entries)
                }

                ForEach(groupedEntries, id: \.title) { section in
                    Section(section.title) {
                        ForEach(section.entries) { entry in
                            Button {
                                selectedEntry = entry
                            } label: {
                                TimelineRow(entry: entry)
                            }
                            .swipeActions {
                                Button("Duplicate") {
                                    store.duplicate(entry: entry, newDate: Date())
                                }
                                .tint(.blue)

                                Button("Delete", role: .destructive) {
                                    store.delete(entry: entry)
                                }
                            }
                        }
                    }
                }
            }
            .navigationTitle("Timeline")
            .sheet(item: $selectedEntry) { entry in
                EditEntryView(entry: entry)
            }
        }
    }

    private var groupedEntries: [TimelineSection] {
        let formatter = DateFormatter()
        formatter.dateFormat = "MMMM YYYY"
        let groups = Dictionary(grouping: store.entries) { entry in
            formatter.string(from: entry.date)
        }

        return groups.map { key, value in
            TimelineSection(title: key, entries: value.sorted { $0.date > $1.date })
        }
        .sorted { $0.title > $1.title }
    }
}

private struct TimelineSection: Identifiable {
    let id = UUID()
    let title: String
    let entries: [WorkEntry]
}

private struct TimelineRow: View {
    let entry: WorkEntry

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text(entry.date, style: .date)
                    .font(.headline)
                Text("\(timeRange) • \(durationText)")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            Text(entry.groupLabel)
                .font(.subheadline)
                .foregroundStyle(.primary)
        }
    }

    private var timeRange: String {
        let formatter = DateFormatter()
        formatter.timeStyle = .short
        return "\(formatter.string(from: entry.startTime)) – \(formatter.string(from: entry.endTime))"
    }

    private var durationText: String {
        let hours = Double(entry.durationMinutes) / 60.0
        return String(format: "%.2f h", hours)
    }
}

private struct WeeklyMonthlySummary: View {
    let entries: [WorkEntry]

    var body: some View {
        HStack(spacing: 16) {
            SummaryCard(title: "This Week", value: totalFor(.week))
            SummaryCard(title: "This Month", value: totalFor(.month))
        }
        .padding(.vertical, 8)
    }

    private func totalFor(_ component: Calendar.Component) -> String {
        let calendar = Calendar.current
        let now = Date()
        let total = entries.filter {
            calendar.isDate($0.date, equalTo: now, toGranularity: component)
        }
        .reduce(0) { $0 + $1.durationMinutes }

        return String(format: "%.1f h", Double(total) / 60.0)
    }
}

private struct SummaryCard: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.title3)
                .fontWeight(.semibold)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(Color(.secondarySystemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }
}

#Preview {
    TimelineView()
        .environmentObject(WorkStore())
}
