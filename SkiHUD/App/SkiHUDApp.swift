import SwiftUI

@main
struct SkiHUDApp: App {
    @StateObject private var viewModel = MountainMapViewModel.livePreview

    var body: some Scene {
        WindowGroup {
            MountainMapScreen(viewModel: viewModel)
        }
    }
}
