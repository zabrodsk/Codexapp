import SwiftUI

@main
struct HoursLoggerApp: App {
    @StateObject private var store = WorkStore()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(store)
        }
    }
}
