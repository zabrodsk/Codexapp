import SwiftUI

struct ContentView: View {
    var body: some View {
        TabView {
            TimelineView()
                .tabItem {
                    Label("Timeline", systemImage: "clock")
                }

            QuickAddView()
                .tabItem {
                    Label("Quick Add", systemImage: "plus.circle.fill")
                }

            TemplatesView()
                .tabItem {
                    Label("Templates", systemImage: "rectangle.grid.1x2")
                }

            StatsView()
                .tabItem {
                    Label("Stats", systemImage: "chart.bar")
                }

            ExportView()
                .tabItem {
                    Label("Export", systemImage: "square.and.arrow.up")
                }
        }
    }
}

#Preview {
    ContentView()
        .environmentObject(WorkStore())
}
