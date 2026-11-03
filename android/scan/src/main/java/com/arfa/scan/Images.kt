package com.arfa.scan

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.net.Uri
import androidx.exifinterface.media.ExifInterface
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import kotlin.math.max

object Images {

    /** A photo made ready to send: upright (its EXIF turn applied), at most
     * maxPx on the long side, JPEG - a 4 MB photo becomes ~400 KB, the
     * slow part on mobile data. */
    fun prepareJpeg(context: Context, uri: Uri, out: File, maxPx: Int = 2400, quality: Int = 88) {
        val resolver = context.contentResolver
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        resolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, bounds) }
            ?: throw IOException("That picture couldn't be opened.")
        var sample = 1
        while (max(bounds.outWidth, bounds.outHeight) / (sample * 2) >= maxPx) sample *= 2
        val decoded = resolver.openInputStream(uri)?.use {
            BitmapFactory.decodeStream(it, null, BitmapFactory.Options().apply { inSampleSize = sample })
        } ?: throw IOException("That picture couldn't be opened.")
        val turn = runCatching {
            resolver.openInputStream(uri)?.use {
                when (ExifInterface(it).getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)) {
                    ExifInterface.ORIENTATION_ROTATE_90 -> 90f
                    ExifInterface.ORIENTATION_ROTATE_180 -> 180f
                    ExifInterface.ORIENTATION_ROTATE_270 -> 270f
                    else -> 0f
                }
            } ?: 0f
        }.getOrDefault(0f)
        val scale = minOf(1f, maxPx.toFloat() / max(decoded.width, decoded.height))
        val matrix = Matrix().apply {
            if (scale < 1f) postScale(scale, scale)
            if (turn != 0f) postRotate(turn)
        }
        val ready = if (scale < 1f || turn != 0f)
            Bitmap.createBitmap(decoded, 0, 0, decoded.width, decoded.height, matrix, true) else decoded
        FileOutputStream(out).use { ready.compress(Bitmap.CompressFormat.JPEG, quality, it) }
        if (ready !== decoded) ready.recycle()
        decoded.recycle()
    }

    /** A small picture of the first page, for the bubble while it sends. */
    fun preview(context: Context, uri: Uri, out: File) = prepareJpeg(context, uri, out, maxPx = 360, quality = 75)

    fun copy(context: Context, uri: Uri, out: File) {
        context.contentResolver.openInputStream(uri)?.use { input -> FileOutputStream(out).use { input.copyTo(it) } }
            ?: throw IOException("That file couldn't be opened.")
    }
}
