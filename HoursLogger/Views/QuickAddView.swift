import SwiftUI

struct QuickAddView: View {
    @EnvironmentObject private var store: WorkStore
    @State private var date = Date()
    @State private var startTime = Date()
    @State private var endTime = Date()
    @State private var groupLabel = ""
    @State private var names = ""
    @State private var note = ""
    @State private var withPerson = ""
    @State private var manualDurationEnabled = false
    @State private var manualDuration = ""

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    DatePicker("Datum", selection: $date, displayedComponents: .date)
                        .datePickerStyle(.compact)

                    VStack(alignment: .leading, spacing: 8) {
                        Text("Time block")
                            .font(.headline)
                        WrapChips(items: store.timeBlocks.map(\.label)) { label in
                            Button(label) {
                                applyTimeBlock(label)
                            }
                            .buttonStyle(.bordered)
                        }
                        Button("Custom time") {
                            startTime = Calendar.current.date(bySettingHour: 8, minute: 0, second: 0, of: date) ?? date
                            endTime = Calendar.current.date(bySettingHour: 14, minute: 0, second: 0, of: date) ?? date
                        }
                        .buttonStyle(.borderedProminent)
                    }

                    VStack(alignment: .leading, spacing: 8) {
                        DatePicker("Od", selection: $startTime, displayedComponents: .hourAndMinute)
                        DatePicker("Do", selection: $endTime, displayedComponents: .hourAndMinute)
                        Text("Duration: \(durationText)")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }

                    VStack(alignment: .leading, spacing: 8) {
                        Text("Group / Session")
                            .font(.headline)
                        TextField("Malý Race tým", text: $groupLabel)
                            .textFieldStyle(.roundedBorder)
                    }

                    VStack(alignment: .leading, spacing: 8) {
                        Text("With")
                            .font(.headline)
                        WrapChips(items: store.withPeople) { person in
                            Button(person) {
                                withPerson = person
                            }
                            .buttonStyle(.bordered)
                        }
                        TextField("With person", text: $withPerson)
                            .textFieldStyle(.roundedBorder)
                    }

                    VStack(alignment: .leading, spacing: 8) {
                        Text("Names")
                            .font(.headline)
                        TextField("Jména dětí", text: $names)
                            .textFieldStyle(.roundedBorder)
                    }

                    VStack(alignment: .leading, spacing: 8) {
                        Text("Note")
                            .font(.headline)
                        TextField("Poznámka", text: $note)
                            .textFieldStyle(.roundedBorder)
                    }

                    Toggle("Manual duration", isOn: $manualDurationEnabled)
                    if manualDurationEnabled {
                        TextField("Minutes", text: $manualDuration)
                            .keyboardType(.numberPad)
                            .textFieldStyle(.roundedBorder)
                    }

                    Button("Save") {
                        saveEntry()
                    }
                    .buttonStyle(.borderedProminent)
                    .frame(maxWidth: .infinity)
                }
                .padding()
            }
            .navigationTitle("Quick Add")
            .onAppear {
                applySuggestion()
            }
        }
    }

    private var durationText: String {
        let minutes = Int(endTime.timeIntervalSince(startTime) / 60)
        return String(format: "%.2f h", Double(max(minutes, 0)) / 60.0)
    }

    private func applyTimeBlock(_ label: String) {
        guard let block = store.timeBlocks.first(where: { $0.label == label }) else { return }
        let calendar = Calendar.current
        startTime = calendar.date(bySettingHour: block.startTime.hour ?? 0, minute: block.startTime.minute ?? 0, second: 0, of: date) ?? date
        endTime = calendar.date(bySettingHour: block.endTime.hour ?? 0, minute: block.endTime.minute ?? 0, second: 0, of: date) ?? date
    }

    private func applySuggestion() {
        if let firstBlock = store.timeBlocks.first {
            applyTimeBlock(firstBlock.label)
        }
        if let last = store.entries.first {
            groupLabel = last.groupLabel
        }
    }

    private func saveEntry() {
        let manualMinutes = manualDurationEnabled ? Int(manualDuration) : nil
        let entry = WorkEntry(
            date: date,
            startTime: startTime,
            endTime: endTime,
            groupLabel: groupLabel.isEmpty ? "New Session" : groupLabel,
            names: names,
            withPerson: withPerson,
            note: note,
            manualDurationMinutes: manualMinutes
        )
        store.add(entry: entry)
        groupLabel = ""
        names = ""
        note = ""
        withPerson = ""
        manualDuration = ""
        manualDurationEnabled = false
    }
}

private struct WrapChips<Item: Hashable, Content: View>: View {
    let items: [Item]
    let content: (Item) -> Content

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 120), alignment: .leading)], spacing: 8) {
            ForEach(items, id: \.self) { item in
                content(item)
            }
        }
    }
}

#Preview {
    QuickAddView()
        .environmentObject(WorkStore())
}
