package com.arfa.scan

import android.app.Application
import coil.ImageLoader
import coil.ImageLoaderFactory

class ScanApp : Application(), ImageLoaderFactory {
    override fun onCreate() {
        super.onCreate()
        Api.session = Session(this)
        Outbox.init(this)
    }

    /** Pictures load through the same signed-in client as everything else. */
    override fun newImageLoader(): ImageLoader =
        ImageLoader.Builder(this).okHttpClient { Api.http }.crossfade(true).build()
}
