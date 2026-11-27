import SwiftUI
import UIKit

@main
struct DilmacApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
                // iOS suspends apps in the background: Dilmaç has to stay in front with the screen on.
                .onAppear { UIApplication.shared.isIdleTimerDisabled = true }
        }
    }
}
