import SwiftUI
import MapKit

struct MountainMapScreen: View {
    @ObservedObject var viewModel: MountainMapViewModel
    @State private var cameraPosition: MapCameraPosition

    init(viewModel: MountainMapViewModel) {
        self.viewModel = viewModel
        _cameraPosition = State(initialValue: .region(MKCoordinateRegion(
            center: viewModel.resort.center,
            span: MKCoordinateSpan(latitudeDelta: 0.06, longitudeDelta: 0.06)
        )))
    }

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Map(position: $cameraPosition) {
                ForEach(viewModel.resort.events) { event in
                    Annotation(event.title, coordinate: event.coordinate) {
                        EventMarkerView(event: event)
                            .onTapGesture {
                                viewModel.selectedEvent = event
                            }
                    }
                }

                MapPolyline(coordinates: viewModel.playerTrail.points.map(\.coordinate))
                    .stroke(.cyan, style: StrokeStyle(lineWidth: 4, lineCap: .round))

                ForEach(viewModel.friendTrails) { trail in
                    MapPolyline(coordinates: trail.points.map(\.coordinate))
                        .stroke(.white.opacity(0.75), style: StrokeStyle(lineWidth: 2, dash: [6, 5]))
                }
            }
            .mapStyle(.hybrid(elevation: .realistic))
            .ignoresSafeArea()

            VStack(alignment: .trailing, spacing: 12) {
                TerrainStatusCard(state: viewModel.terrainState)
                if !viewModel.isMiniMapCollapsed {
                    LiveHUDCard(hud: viewModel.hudState, resort: viewModel.resort)
                }

                Button(viewModel.isMiniMapCollapsed ? "Expand HUD" : "Collapse HUD") {
                    viewModel.isMiniMapCollapsed.toggle()
                }
                .font(.caption.bold())
                .buttonStyle(.borderedProminent)
            }
            .padding()
        }
        .task {
            await viewModel.refreshLiveLayers()
        }
        .sheet(item: $viewModel.selectedEvent) { event in
            EventPreviewSheet(
                event: event,
                completeAction: viewModel.completeSelectedEvent
            )
        }
    }
}

private struct EventMarkerView: View {
    let event: MapEvent

    var body: some View {
        ZStack {
            Circle()
                .fill(event.isCompleted ? Color.green : iconColor.opacity(0.8))
                .frame(width: 34, height: 34)
            Image(systemName: iconName)
                .foregroundStyle(.white)
                .font(.headline)
        }
        .overlay(Circle().stroke(.white.opacity(0.8), lineWidth: 1))
    }

    private var iconName: String {
        switch event.type {
        case .gate: return "flag.checkered"
        case .accolade: return "trophy.fill"
        case .playlist: return "sparkles"
        case .social: return "person.2.fill"
        }
    }

    private var iconColor: Color {
        switch event.type {
        case .gate: return .orange
        case .accolade: return .yellow
        case .playlist: return .purple
        case .social: return .blue
        }
    }
}
