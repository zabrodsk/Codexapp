import SwiftUI

struct EditEntryView: View {
    @EnvironmentObject private var store: WorkStore
    @Environment(\.dismiss) private var dismiss
    @State private var entry: WorkEntry
    @State private var manualDuration = ""
    @State private var manualDurationEnabled = false

    init(entry: WorkEntry) {
        _entry = State(initialValue: entry)
        _manualDuration = State(initialValue: entry.manualDurationMinutes.map(String.init) ?? "")
        _manualDurationEnabled = State(initialValue: entry.manualDurationMinutes != nil)
    }

    var body: some View {
        NavigationStack {
            Form {
                DatePicker("Datum", selection: $entry.date, displayedComponents: .date)
                DatePicker("Od", selection: $entry.startTime, displayedComponents: .hourAndMinute)
                DatePicker("Do", selection: $entry.endTime, displayedComponents: .hourAndMinute)
                Text("Duration: \(durationText)")
                    .foregroundStyle(.secondary)

                TextField("Group", text: $entry.groupLabel)
                TextField("Names", text: $entry.names)
                TextField("With", text: $entry.withPerson)
                TextField("Note", text: $entry.note)

                Toggle("Manual duration", isOn: $manualDurationEnabled)
                if manualDurationEnabled {
                    TextField("Minutes", text: $manualDuration)
                        .keyboardType(.numberPad)
                }
            }
            .navigationTitle("Edit Entry")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        entry.manualDurationMinutes = manualDurationEnabled ? Int(manualDuration) : nil
                        store.update(entry: entry)
                        dismiss()
                    }
                }
            }
        }
    }

    private var durationText: String {
        let minutes = Int(entry.endTime.timeIntervalSince(entry.startTime) / 60)
        return String(format: "%.2f h", Double(max(minutes, 0)) / 60.0)
    }
}

#Preview {
    EditEntryView(entry: WorkEntry(
        date: Date(),
        startTime: Date(),
        endTime: Date().addingTimeInterval(2_700),
        groupLabel: "Malý Race tým"
    ))
    .environmentObject(WorkStore())
}
