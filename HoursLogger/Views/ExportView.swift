import SwiftUI

struct ExportView: View {
    @EnvironmentObject private var store: WorkStore

    var body: some View {
        NavigationStack {
            List {
                Section("Export") {
                    Button("Export CSV") {
                        export(format: "CSV")
                    }
                    Button("Export XLSX") {
                        export(format: "XLSX")
                    }
                }

                Section("Backup") {
                    Toggle(isOn: .constant(true)) {
                        VStack(alignment: .leading) {
                            Text("iCloud Sync")
                            Text("Keep data private with device-only storage.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }

                Section("Preview") {
                    ForEach(store.entries) { entry in
                        Text("\(entry.date.formatted(date: .numeric, time: .omitted)) • \(entry.groupLabel)")
                            .font(.subheadline)
                    }
                }
            }
            .navigationTitle("Export")
        }
    }

    private func export(format: String) {
        print("Export \(format) with \(store.entries.count) entries")
    }
}

#Preview {
    ExportView()
        .environmentObject(WorkStore())
}
