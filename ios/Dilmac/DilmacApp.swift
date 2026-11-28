import SwiftUI
#if os(iOS)
import UIKit
#endif

@main
struct DilmacApp: App {
    var body: some Scene {
        WindowGroup {
            #if os(iOS)
            ContentView()
                // iOS suspends apps in the background: Dilmaç has to stay in front with the screen on.
                .onAppear { UIApplication.shared.isIdleTimerDisabled = true }
            #else
            ContentView() // the Mac stays awake while a session runs (AppModel.connect)
            #endif
        }
    }
}
