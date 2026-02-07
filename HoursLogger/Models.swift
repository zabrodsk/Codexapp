import Foundation

struct WorkEntry: Identifiable, Hashable {
    var id: UUID
    var date: Date
    var startTime: Date
    var endTime: Date
    var groupLabel: String
    var names: String
    var withPerson: String
    var note: String
    var manualDurationMinutes: Int?

    init(
        id: UUID = UUID(),
        date: Date,
        startTime: Date,
        endTime: Date,
        groupLabel: String,
        names: String = "",
        withPerson: String = "",
        note: String = "",
        manualDurationMinutes: Int? = nil
    ) {
        self.id = id
        self.date = date
        self.startTime = startTime
        self.endTime = endTime
        self.groupLabel = groupLabel
        self.names = names
        self.withPerson = withPerson
        self.note = note
        self.manualDurationMinutes = manualDurationMinutes
    }

    var durationMinutes: Int {
        if let manualDurationMinutes {
            return manualDurationMinutes
        }
        let minutes = Int(endTime.timeIntervalSince(startTime) / 60)
        return max(minutes, 0)
    }
}

struct WorkTemplate: Identifiable, Hashable {
    let id: UUID
    var title: String
    var groupLabel: String
    var startTime: Date
    var endTime: Date
    var withPerson: String
    var note: String

    init(
        id: UUID = UUID(),
        title: String,
        groupLabel: String,
        startTime: Date,
        endTime: Date,
        withPerson: String = "",
        note: String = ""
    ) {
        self.id = id
        self.title = title
        self.groupLabel = groupLabel
        self.startTime = startTime
        self.endTime = endTime
        self.withPerson = withPerson
        self.note = note
    }
}

struct TimeBlock: Identifiable, Hashable {
    let id = UUID()
    let label: String
    let startTime: DateComponents
    let endTime: DateComponents
}
