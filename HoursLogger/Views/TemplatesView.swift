import SwiftUI

struct TemplatesView: View {
    @EnvironmentObject private var store: WorkStore
    @State private var selectedTemplate: WorkTemplate?

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach(store.templates) { template in
                        Button {
                            selectedTemplate = template
                        } label: {
                            VStack(alignment: .leading, spacing: 6) {
                                Text(template.title)
                                    .font(.headline)
                                Text("\(timeRange(for: template)) • \(template.groupLabel)")
                                    .font(.subheadline)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                } header: {
                    Text("Tap to log with template")
                }
            }
            .navigationTitle("Templates")
            .sheet(item: $selectedTemplate) { template in
                TemplateQuickAddView(template: template)
            }
        }
    }

    private func timeRange(for template: WorkTemplate) -> String {
        let formatter = DateFormatter()
        formatter.timeStyle = .short
        return "\(formatter.string(from: template.startTime)) – \(formatter.string(from: template.endTime))"
    }
}

private struct TemplateQuickAddView: View {
    @EnvironmentObject private var store: WorkStore
    @Environment(\.dismiss) private var dismiss
    let template: WorkTemplate
    @State private var date = Date()

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 16) {
                DatePicker("Datum", selection: $date, displayedComponents: .date)
                    .datePickerStyle(.compact)
                TemplateDetailRow(title: "Group", value: template.groupLabel)
                TemplateDetailRow(title: "Time", value: timeRange)
                if !template.withPerson.isEmpty {
                    TemplateDetailRow(title: "With", value: template.withPerson)
                }
                if !template.note.isEmpty {
                    TemplateDetailRow(title: "Note", value: template.note)
                }
                Button("Save") {
                    let entry = WorkEntry(
                        date: date,
                        startTime: template.startTime,
                        endTime: template.endTime,
                        groupLabel: template.groupLabel,
                        withPerson: template.withPerson,
                        note: template.note
                    )
                    store.add(entry: entry)
                    dismiss()
                }
                .buttonStyle(.borderedProminent)
                .frame(maxWidth: .infinity)
                Spacer()
            }
            .padding()
            .navigationTitle(template.title)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") {
                        dismiss()
                    }
                }
            }
        }
    }

    private var timeRange: String {
        let formatter = DateFormatter()
        formatter.timeStyle = .short
        return "\(formatter.string(from: template.startTime)) – \(formatter.string(from: template.endTime))"
    }
}

private struct TemplateDetailRow: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.headline)
        }
    }
}

#Preview {
    TemplatesView()
        .environmentObject(WorkStore())
}
