package com.dilmac.tv;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Starts the service by itself when the TV boots and when the app is updated (no need to open the app every time;
 * subtitles work right after an update pushed from the PC). */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        String a = intent.getAction();
        if (Intent.ACTION_BOOT_COMPLETED.equals(a) || Intent.ACTION_MY_PACKAGE_REPLACED.equals(a)) {
            try {
                context.startForegroundService(new Intent(context, OverlayService.class));
            } catch (Exception ignored) { }
        }
    }
}
