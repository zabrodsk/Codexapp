import Foundation

final class WorkStore: ObservableObject {
    @Published var entries: [WorkEntry]
    @Published var templates: [WorkTemplate]

    let timeBlocks: [TimeBlock]
    let withPeople: [String]

    init(now: Date = Date()) {
        let calendar = Calendar.current
        func time(_ hour: Int, _ minute: Int) -> Date {
            calendar.date(bySettingHour: hour, minute: minute, second: 0, of: now) ?? now
        }

        timeBlocks = [
            TimeBlock(label: "08:30–11:00", startTime: .init(hour: 8, minute: 30), endTime: .init(hour: 11, minute: 0)),
            TimeBlock(label: "08:30–10:30", startTime: .init(hour: 8, minute: 30), endTime: .init(hour: 10, minute: 30)),
            TimeBlock(label: "11:30–13:30", startTime: .init(hour: 11, minute: 30), endTime: .init(hour: 13, minute: 30)),
            TimeBlock(label: "08:00–14:00", startTime: .init(hour: 8, minute: 0), endTime: .init(hour: 14, minute: 0))
        ]

        withPeople = ["Andy", "Nick", "Danča"]

        templates = [
            WorkTemplate(
                title: "Malý Race tým",
                groupLabel: "Malý Race tým",
                startTime: time(8, 30),
                endTime: time(11, 0)
            ),
            WorkTemplate(
                title: "Velký race team",
                groupLabel: "Velký race team",
                startTime: time(8, 30),
                endTime: time(11, 0)
            ),
            WorkTemplate(
                title: "Němci pokročilejší",
                groupLabel: "Němci pokročilejší",
                startTime: time(11, 30),
                endTime: time(13, 30),
                withPerson: "Nick"
            ),
            WorkTemplate(
                title: "Závody",
                groupLabel: "Závody",
                startTime: time(8, 0),
                endTime: time(14, 0)
            )
        ]

        entries = [
            WorkEntry(
                date: now,
                startTime: time(8, 30),
                endTime: time(11, 0),
                groupLabel: "Malý Race tým",
                withPerson: "Andy"
            )
        ]
    }

    func add(entry: WorkEntry) {
        entries.insert(entry, at: 0)
    }

    func update(entry: WorkEntry) {
        guard let index = entries.firstIndex(where: { $0.id == entry.id }) else { return }
        entries[index] = entry
    }

    func delete(entry: WorkEntry) {
        entries.removeAll { $0.id == entry.id }
    }

    func duplicate(entry: WorkEntry, newDate: Date) {
        var copy = entry
        copy.id = UUID()
        copy.date = newDate
        add(entry: copy)
    }
}

