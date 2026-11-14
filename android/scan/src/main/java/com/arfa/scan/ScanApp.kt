package com.arfa.scan

import android.app.Application
import androidx.appcompat.app.AppCompatDelegate
import coil.ImageLoader
import coil.ImageLoaderFactory

class ScanApp : Application(), ImageLoaderFactory {
    override fun onCreate() {
        super.onCreate()
        Api.session = Session(this)
        applyTheme(Api.session.theme)
        Outbox.init(this)
    }

    companion object {
        /** Day, night, or whatever the phone is set to (the default). */
        fun applyTheme(theme: String) {
            AppCompatDelegate.setDefaultNightMode(
                when (theme) {
                    "light" -> AppCompatDelegate.MODE_NIGHT_NO
                    "dark" -> AppCompatDelegate.MODE_NIGHT_YES
                    else -> AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM
                }
            )
        }
    }

    /** Pictures load through the same signed-in client as everything else. */
    override fun newImageLoader(): ImageLoader =
        ImageLoader.Builder(this).okHttpClient { Api.http }.crossfade(true).build()
}
