package com.dilmac.tv;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.provider.Settings;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.util.Collections;

/** Status screen: IP address, permission state, test button. Starts the service when opened. */
public class MainActivity extends Activity {
    private TextView status;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER);
        root.setPadding(80, 60, 80, 60);
        root.setBackgroundColor(Color.rgb(18, 18, 24));

        TextView title = new TextView(this);
        title.setText("Dilmaç");
        title.setTextSize(36);
        title.setTextColor(Color.WHITE);
        title.setGravity(Gravity.CENTER);
        root.addView(title);

        status = new TextView(this);
        status.setTextSize(22);
        status.setTextColor(Color.rgb(210, 210, 220));
        status.setGravity(Gravity.CENTER);
        status.setPadding(0, 40, 0, 40);
        root.addView(status);

        Button test = new Button(this);
        test.setText("Deneme altyazısı göster");
        test.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { startOverlay(OverlayService.ACTION_TEST); }
        });
        root.addView(test);

        Button perm = new Button(this);
        perm.setText("Üstte gösterme izni ayarı");
        perm.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                try {
                    startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                            Uri.parse("package:" + getPackageName())));
                } catch (Exception e) {
                    status.setText("Bu TV'de izin ekranı yok. PC'den kurulum betiği izni verir (adb).");
                }
            }
        });
        root.addView(perm);

        Button home = new Button(this);
        home.setText("Arka planda çalıştır (ana ekrana dön)");
        home.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { moveTaskToBack(true); }
        });
        root.addView(home);

        setContentView(root);
        test.requestFocus();

        if (getIntent() != null && getIntent().getBooleanExtra("test", false)) {
            startOverlay(OverlayService.ACTION_TEST);
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        startOverlay(null);
        refresh();
    }

    private void startOverlay(String action) {
        Intent i = new Intent(this, OverlayService.class);
        if (action != null) i.setAction(action);
        startForegroundService(i);
        status.postDelayed(new Runnable() { @Override public void run() { refresh(); } }, 500);
    }

    private void refresh() {
        boolean perm = Settings.canDrawOverlays(this);
        StringBuilder sb = new StringBuilder();
        sb.append("PC'deki config.json için TV IP adresi:\n");
        String ip = localIp();
        sb.append(ip == null ? "(ağ bağlantısı yok)" : ip).append("\n\n");
        sb.append("Port: ").append(OverlayService.PORT).append("\n");
        sb.append("Üstte gösterme izni: ").append(perm ? "VAR" : "YOK").append("\n");
        sb.append("Servis: ").append(OverlayService.running ? "çalışıyor" : "durdu");
        if (!OverlayService.lastError.isEmpty()) sb.append("\nUyarı: ").append(OverlayService.lastError);
        status.setText(sb.toString());
    }

    private static String localIp() {
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                for (InetAddress a : Collections.list(ni.getInetAddresses())) {
                    if (a instanceof Inet4Address && !a.isLoopbackAddress()) return a.getHostAddress();
                }
            }
        } catch (Exception ignored) { }
        return null;
    }
}
