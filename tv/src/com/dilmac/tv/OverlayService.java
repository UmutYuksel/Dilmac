package com.dilmac.tv;

import android.animation.ArgbEvaluator;
import android.animation.ValueAnimator;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.Point;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.provider.Settings;
import android.util.Log;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.view.animation.DecelerateInterpolator;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;

/**
 * Listens on TCP port 8787 on the local network; every line from the PC is one JSON message:
 *   {"type":"show","id":"speech","text":"...","ttl":10,"new":true,
 *    "box":[x,y,w,h],"lines":1,"lineh":0.029,"opacity":0.9,"scale":1,"max_width":0.8,"old_secs":3,
 *    "color":"#FF9AD5","fade_ms":180}
 *   {"type":"clear"}  or  {"type":"clear","id":"objective"}
 * box: where the English text is in the game picture (0-1). The box sits exactly on top of it, wide and tall enough to
 * cover it; the text size comes from lineh (share of the screen height). Without box the old layout is used:
 * "bottom" dp from the bottom, "size" sp text.
 * Every id is a separate box (speech, objective, k0..k5). With "new" the previous line stays dimmed above the box for
 * "old_secs" seconds.
 * "color" (optional, "#RRGGBB"): the speaker's color (Lucia, Jason, pedestrian...); white otherwise.
 * "fade_ms" (optional, default 180): the box fades in/out, new text fades in, a moved box slides; 0 = instant.
 * Messages from older PC versions (opak, olcek, genis, eski, yeni, renk, gecis) are still understood.
 */
public class OverlayService extends Service {
    public static final int PORT = 8787;
    public static final String ACTION_TEST = "com.dilmac.tv.TEST";
    private static final String TAG = "Dilmac";
    private static final String CHANNEL = "overlay";
    private static final int OLD_LINE_COLOR = Color.rgb(170, 170, 178);
    /** Background of the subtitle pills: a very dark blue-black, softer than pure black. */
    private static final int PILL_R = 12, PILL_G = 14, PILL_B = 18;
    /** The previous line: smaller and dimmer than the current one, in its own pill above it. */
    private static final float OLD_SCALE = 0.8f, OLD_ALPHA = 0.75f;
    private static final DecelerateInterpolator EASE = new DecelerateInterpolator(1.5f);

    static volatile boolean running;
    static volatile String lastError = "";

    private WindowManager wm;
    private Handler main;
    private ServerSocket server;
    private final Map<String, Panel> panels = new HashMap<>();

    /** A field under its current name, or under the name older PC versions used. */
    private static double num(JSONObject m, String key, String oldKey, double def) {
        return m.has(key) ? m.optDouble(key, def) : m.optDouble(oldKey, def);
    }

    private static boolean flag(JSONObject m, String key, String oldKey) {
        return m.has(key) ? m.optBoolean(key, false) : m.optBoolean(oldKey, false);
    }

    private static String str(JSONObject m, String key, String oldKey) {
        return m.has(key) ? m.optString(key, "") : m.optString(oldKey, "");
    }

    /** One box on screen: a dimmed old line on top, the current line below. */
    private final class Panel {
        final LinearLayout root;
        final TextView old;
        final TextView cur;
        final GradientDrawable curBg = new GradientDrawable();
        final GradientDrawable oldBg = new GradientDrawable();
        final WindowManager.LayoutParams lp;
        boolean added;
        boolean visible;
        long fadeMs = 180;
        ValueAnimator slide;
        ValueAnimator colorAnim;
        final Runnable hide = new Runnable() {
            @Override public void run() { fadeOut(); }
        };
        final Runnable hideOld = new Runnable() {
            @Override public void run() {
                if (fadeMs <= 0 || old.getVisibility() != View.VISIBLE) {
                    old.setVisibility(View.GONE);
                    return;
                }
                old.animate().alpha(0f).setDuration(fadeOutMs()).setInterpolator(EASE).withEndAction(new Runnable() {
                    @Override public void run() { old.setVisibility(View.GONE); old.setAlpha(OLD_ALPHA); }
                }).start();
            }
        };

        Panel() {
            // The window itself is transparent; each line has its own pill (the current one also covers the English).
            root = new LinearLayout(OverlayService.this);
            root.setOrientation(LinearLayout.VERTICAL);
            root.setGravity(Gravity.CENTER_HORIZONTAL);
            old = text(OLD_LINE_COLOR);
            old.setBackground(oldBg);
            old.setAlpha(OLD_ALPHA);
            old.setVisibility(View.GONE);
            cur = text(Color.WHITE);
            cur.setBackground(curBg);
            LinearLayout.LayoutParams wrap = new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
            root.addView(old, wrap);
            root.addView(cur, new LinearLayout.LayoutParams(wrap));
            root.setVisibility(View.GONE);
            // No FLAG_NOT_TOUCHABLE: on Android 12+ an overlay that passes touches through is drawn at most 80% opaque
            // and the English subtitle showed through. A TV has no touch; NOT_FOCUSABLE is enough for the remote to
            // keep controlling the game/app.
            lp = new WindowManager.LayoutParams(
                    WindowManager.LayoutParams.WRAP_CONTENT,
                    WindowManager.LayoutParams.WRAP_CONTENT,
                    WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                    WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                            | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                    PixelFormat.TRANSLUCENT);
            lp.gravity = Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL;
            lp.y = dp(60);
        }

        private TextView text(int color) {
            TextView t = new TextView(OverlayService.this);
            t.setTextColor(color);
            t.setGravity(Gravity.CENTER);
            t.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
            t.setShadowLayer(3f, 1.5f, 1.5f, Color.BLACK);
            t.setIncludeFontPadding(false);
            t.setLineSpacing(0f, 1.08f);
            return t;
        }

        long fadeOutMs() { return Math.round(fadeMs * 1.4); }

        /** Make visible: fades in if hidden (or fading out). */
        void fadeIn() {
            visible = true;
            root.animate().cancel();
            if (root.getVisibility() != View.VISIBLE) {
                root.setAlpha(fadeMs > 0 ? 0f : 1f);
                root.setVisibility(View.VISIBLE);
            }
            if (fadeMs > 0 && root.getAlpha() < 1f) {
                root.animate().alpha(1f).setDuration(fadeMs).setInterpolator(EASE).start();
            } else {
                root.setAlpha(1f);
            }
        }

        /** Hide: fades out first, then goes away (fadeIn() brings it back if new text comes meanwhile). */
        void fadeOut() {
            visible = false;
            main.removeCallbacks(hideOld);
            root.animate().cancel();
            if (fadeMs <= 0 || root.getVisibility() != View.VISIBLE) {
                remove();
                return;
            }
            root.animate().alpha(0f).setDuration(fadeOutMs()).setInterpolator(EASE).withEndAction(new Runnable() {
                @Override public void run() { if (!visible) remove(); }
            }).start();
        }

        private void remove() {
            root.setVisibility(View.GONE);
            root.setAlpha(1f);
            old.animate().cancel();
            old.setVisibility(View.GONE);
            old.setAlpha(OLD_ALPHA);
            if (colorAnim != null) colorAnim.cancel();
            cur.setText("");
        }

        /** Text color, blended over a short time if the same line only changes color (the voice was recognized). */
        void setColor(int color, boolean blend) {
            if (colorAnim != null) colorAnim.cancel();
            int from = cur.getCurrentTextColor();
            if (!blend || from == color || fadeMs <= 0) {
                cur.setTextColor(color);
                return;
            }
            colorAnim = ValueAnimator.ofObject(new ArgbEvaluator(), from, color);
            colorAnim.setDuration(Math.max(250, fadeMs * 2));
            colorAnim.addUpdateListener(new ValueAnimator.AnimatorUpdateListener() {
                @Override public void onAnimationUpdate(ValueAnimator a) { cur.setTextColor((Integer) a.getAnimatedValue()); }
            });
            colorAnim.start();
        }

        /** Move the box to its new place: sliding if it is already on screen (animate), otherwise at once. */
        void place(int x, int y, boolean animate) {
            if (slide != null) slide.cancel();
            final int x0 = lp.x, y0 = lp.y;
            if (!animate || fadeMs <= 0 || (Math.abs(x - x0) < 3 && Math.abs(y - y0) < 3)) {
                lp.x = x;
                lp.y = y;
                relayout();
                return;
            }
            final int dx = x - x0, dy = y - y0;
            relayout();  // new text and size at the old place first, then it slides
            slide = ValueAnimator.ofFloat(0f, 1f);
            slide.setDuration(fadeMs);
            slide.setInterpolator(EASE);
            slide.addUpdateListener(new ValueAnimator.AnimatorUpdateListener() {
                @Override public void onAnimationUpdate(ValueAnimator a) {
                    float t = (float) a.getAnimatedValue();
                    lp.x = x0 + Math.round(dx * t);
                    lp.y = y0 + Math.round(dy * t);
                    relayout();
                }
            });
            slide.start();
        }

        void relayout() {
            if (!added) return;
            try {
                wm.updateViewLayout(root, lp);
            } catch (Exception e) {
                Log.w(TAG, "Layout update failed: " + e.getMessage());
            }
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        main = new Handler(Looper.getMainLooper());
        startForeground(1, notification());
        running = true;
        wm = (WindowManager) getSystemService(WINDOW_SERVICE);
        if (!Settings.canDrawOverlays(this)) {
            lastError = "Üstte gösterme izni yok";
            Log.w(TAG, lastError);
        }
        startServer();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_TEST.equals(intent.getAction())) {
            try {
                JSONObject m = new JSONObject();
                m.put("type", "show");
                m.put("text", "Merhaba! Türkçe altyazılar burada, İngilizcenin üstünde görünecek.");
                m.put("ttl", 6);
                m.put("box", new JSONArray(new double[]{0.33, 0.886, 0.34, 0.029}));
                m.put("lines", 1);
                m.put("lineh", 0.029);
                show(m);
            } catch (Exception e) {
                Log.w(TAG, "Test caption failed: " + e.getMessage());
            }
        }
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public void onDestroy() {
        running = false;
        try { if (server != null) server.close(); } catch (Exception ignored) { }
        for (Panel p : panels.values()) {
            if (p.added) {
                try { wm.removeView(p.root); } catch (Exception ignored) { }
            }
        }
        panels.clear();
        super.onDestroy();
    }

    private Notification notification() {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        nm.createNotificationChannel(new NotificationChannel(CHANNEL, "Altyazı", NotificationManager.IMPORTANCE_LOW));
        return new Notification.Builder(this, CHANNEL)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle("Dilmaç çalışıyor")
                .setContentText("Port " + PORT)
                .build();
    }

    private int dp(float v) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics()));
    }

    /** Called on the main thread. Creates the box and adds it to the window if missing; null without permission. */
    private Panel panel(String id) {
        Panel p = panels.get(id);
        if (p != null) return p;
        if (!Settings.canDrawOverlays(this)) {
            lastError = "Üstte gösterme izni yok";
            return null;
        }
        p = new Panel();
        try {
            wm.addView(p.root, p.lp);
            p.added = true;
            lastError = "";
        } catch (Exception e) {
            lastError = "Katman eklenemedi: " + e.getMessage();
            Log.e(TAG, lastError, e);
            return null;
        }
        panels.put(id, p);
        return p;
    }

    private void show(final JSONObject m) {
        main.post(new Runnable() {
            @Override public void run() {
                try {
                    apply(m);
                } catch (Exception e) {
                    Log.w(TAG, "Could not show: " + e.getMessage());
                }
            }
        });
    }

    private void apply(JSONObject m) {
        Panel p = panel(m.optString("id", "speech"));
        if (p == null) return;
        String text = m.optString("text");
        double ttl = m.optDouble("ttl", 10);
        boolean isNew = flag(m, "new", "yeni");
        double opacity = Math.max(0, Math.min(1, num(m, "opacity", "opak", 1.0)));
        double scale = num(m, "scale", "olcek", 1.0);
        double maxWidth = num(m, "max_width", "genis", 0.8);
        double oldSecs = num(m, "old_secs", "eski", 3.0);
        p.fadeMs = Math.max(0, Math.min(1000, Math.round(num(m, "fade_ms", "gecis", 180))));
        int tx, ty;  // where the box goes

        Point scr = new Point();
        wm.getDefaultDisplay().getRealSize(scr);
        float W = scr.x, H = scr.y;
        int alpha = (int) Math.round(opacity * 255);
        p.curBg.setColor(Color.argb(alpha, PILL_R, PILL_G, PILL_B));
        p.oldBg.setColor(Color.argb(alpha, PILL_R, PILL_G, PILL_B));
        int maxW, padX;

        JSONArray box = m.optJSONArray("box");
        if (box != null && box.length() == 4) {
            float bx = (float) box.optDouble(0) * W, by = (float) box.optDouble(1) * H;
            float bw = (float) box.optDouble(2) * W, bh = (float) box.optDouble(3) * H;
            int lines = Math.max(1, m.optInt("lines", 1));
            float lineH = m.has("lineh") ? (float) m.optDouble("lineh") * H : bh / lines;
            float px = (float) (lineH * scale);
            float engRow = bh / lines;  // height of one English row
            int coverX = Math.round(engRow * 0.6f), coverY = Math.round(engRow * 0.3f);
            padX = Math.round(px * 0.5f);
            int padY = Math.round(px * 0.24f);
            styleLines(p, px, padX, padY);
            maxW = Math.round(Math.max(bw + 2 * coverX, (float) maxWidth * W));
            // The current pill covers all of the English text: at least as wide and tall as it, plus a margin.
            p.cur.setMinWidth(Math.round(bw + 2 * coverX));
            p.cur.setMinHeight(Math.round(bh + 2 * coverY));
            tx = Math.round(bx + bw / 2 - W / 2);
            ty = Math.max(0, Math.round(H - (by + bh) - coverY));
        } else {
            int size = m.optInt("size", 30);
            int bottom = m.optInt("bottom", 60);
            float px = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP, size, getResources().getDisplayMetrics());
            padX = Math.round(px * 0.5f);
            styleLines(p, px, padX, Math.round(px * 0.24f));
            maxW = Math.round((float) maxWidth * W);
            p.cur.setMinWidth(0);
            p.cur.setMinHeight(0);
            tx = 0;
            ty = dp(bottom);
        }

        // The speaker's color (e.g. "#FF8AC8"); white otherwise.
        int color = Color.WHITE;
        String cs = str(m, "color", "renk");
        if (cs.length() > 0) {
            try { color = Color.parseColor(cs); } catch (IllegalArgumentException ignored) { }
        }

        String now = p.cur.getText().toString();
        boolean wasVisible = p.visible;
        boolean changed = !now.equals(text);
        if (isNew && oldSecs > 0 && wasVisible && now.length() > 0 && changed) {
            // A subtitle following another: the previous line stays dimmed on top for a while (in its speaker's color).
            p.old.setTextColor(p.cur.getCurrentTextColor());
            p.old.setText(now);
            p.old.animate().cancel();
            p.old.setAlpha(OLD_ALPHA);
            p.old.setVisibility(View.VISIBLE);
            main.removeCallbacks(p.hideOld);
            main.postDelayed(p.hideOld, Math.round(oldSecs * 1000));
        } else if (!wasVisible) {
            main.removeCallbacks(p.hideOld);
            p.old.animate().cancel();
            p.old.setVisibility(View.GONE);
            p.old.setAlpha(OLD_ALPHA);
        }
        p.setColor(color, wasVisible && !changed);
        p.cur.setText(text);
        p.cur.setMaxWidth(maxW);
        p.old.setMaxWidth(maxW);
        // Explicit window width. With WRAP_CONTENT Android first measures an overlay window at dialog width (~880 px
        // here) and keeps it if the content fits by wrapping, so a line that would fit on one row was broken in two.
        int curW = Math.min(maxW, Math.max(p.cur.getMinWidth(), textWidth(p.cur, text) + 2 * padX));
        int oldW = p.old.getVisibility() == View.VISIBLE
                ? Math.min(maxW, textWidth(p.old, p.old.getText().toString()) + 2 * Math.round(padX * OLD_SCALE)) : 0;
        p.lp.width = Math.max(curW, oldW);
        if (wasVisible && changed && p.fadeMs > 0) {
            // The box is already on screen: only the new text fades in (the box does not blink).
            p.cur.animate().cancel();
            p.cur.setAlpha(0.15f);
            p.cur.animate().alpha(1f).setDuration(p.fadeMs).setInterpolator(EASE).start();
        } else if (!wasVisible) {
            p.cur.animate().cancel();
            p.cur.setAlpha(1f);
        }
        p.fadeIn();
        p.place(tx, ty, wasVisible);
        main.removeCallbacks(p.hide);
        if (ttl > 0) main.postDelayed(p.hide, Math.round(ttl * 1000));
    }

    /** Text size, padding and corner radius of both pills (the old one smaller), and the gap between them. */
    private void styleLines(Panel p, float px, int padX, int padY) {
        p.cur.setTextSize(TypedValue.COMPLEX_UNIT_PX, px);
        p.old.setTextSize(TypedValue.COMPLEX_UNIT_PX, px * OLD_SCALE);
        p.cur.setPadding(padX, padY, padX, padY);
        p.old.setPadding(Math.round(padX * OLD_SCALE), Math.round(padY * OLD_SCALE), Math.round(padX * OLD_SCALE),
                Math.round(padY * OLD_SCALE));
        p.curBg.setCornerRadius(px * 0.33f);
        p.oldBg.setCornerRadius(px * OLD_SCALE * 0.33f);
        LinearLayout.LayoutParams olp = (LinearLayout.LayoutParams) p.old.getLayoutParams();
        olp.bottomMargin = Math.round(px * 0.3f);
        p.old.setLayoutParams(olp);
    }

    /** Width of the text on one row in the TextView's current font (no padding). */
    private static int textWidth(TextView t, String s) {
        return (int) Math.ceil(t.getPaint().measureText(s == null ? "" : s));
    }

    private void clear(final String id) {
        main.post(new Runnable() {
            @Override public void run() {
                for (Map.Entry<String, Panel> e : panels.entrySet()) {
                    if (id == null || id.equals(e.getKey())) {
                        main.removeCallbacks(e.getValue().hide);
                        e.getValue().fadeOut();
                    }
                }
            }
        });
    }

    private void startServer() {
        new Thread(new Runnable() {
            @Override public void run() {
                try {
                    server = new ServerSocket();
                    server.setReuseAddress(true);
                    server.bind(new InetSocketAddress(PORT));
                    while (running) {
                        final Socket client = server.accept();
                        new Thread(new Runnable() {
                            @Override public void run() { handle(client); }
                        }).start();
                    }
                } catch (Exception e) {
                    if (running) {
                        lastError = "Sunucu hatası: " + e.getMessage();
                        Log.e(TAG, lastError, e);
                    }
                }
            }
        }, "subtitle-server").start();
    }

    private void handle(Socket client) {
        try {
            client.setTcpNoDelay(true);
            BufferedReader in = new BufferedReader(new InputStreamReader(client.getInputStream(), StandardCharsets.UTF_8));
            String line;
            while ((line = in.readLine()) != null) {
                if (line.length() > 8000) continue;
                try {
                    JSONObject msg = new JSONObject(line);
                    String type = msg.optString("type");
                    if ("show".equals(type)) {
                        show(msg);
                    } else if ("clear".equals(type)) {
                        clear(msg.has("id") ? msg.optString("id") : null);
                    }
                } catch (Exception e) {
                    Log.w(TAG, "Invalid message: " + e.getMessage());
                }
            }
        } catch (Exception e) {
            Log.i(TAG, "Connection closed: " + e.getMessage());
        } finally {
            try { client.close(); } catch (Exception ignored) { }
        }
    }
}
