package com.arfa.warehouse.scan

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Matrix
import android.media.AudioManager
import android.media.ToneGenerator
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.util.Size
import androidx.annotation.OptIn
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.ExperimentalGetImage
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.latin.TextRecognizerOptions
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.UUID
import java.util.concurrent.Executors

/** One camera frame's reading: every barcode's value, the text (only when
 * asked for), and the frame itself - snapshot() is valid only during the
 * call (the frame is released right after). */
class Frame(val barcodes: List<String>, val text: String?, val snapshot: () -> Bitmap?)

/** The frames from the back camera, read on the phone with ML Kit (bundled
 * models: no signal needed). Barcodes on every frame; text only while
 * wantText() says so (it is slower). */
private class LabelAnalyzer(
    private val paused: () -> Boolean,
    private val wantText: () -> Boolean,
    private val onFrame: (Frame) -> Unit,
) : ImageAnalysis.Analyzer {
    private val barcodes = BarcodeScanning.getClient()
    private val texts = TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS)

    @OptIn(ExperimentalGetImage::class)
    override fun analyze(proxy: ImageProxy) {
        val media = proxy.image
        if (paused() || media == null) {
            proxy.close()
            return
        }
        val input = InputImage.fromMediaImage(media, proxy.imageInfo.rotationDegrees)
        val snapshot = { runCatching { upright(proxy) }.getOrNull() }
        barcodes.process(input)
            .addOnSuccessListener { found ->
                val values = found.mapNotNull { it.rawValue?.trim() }.filter { it.isNotEmpty() }
                if (wantText()) {
                    texts.process(input)
                        .addOnSuccessListener { t -> deliver(Frame(values, t.text, snapshot), proxy) }
                        .addOnFailureListener { deliver(Frame(values, null, snapshot), proxy) }
                } else {
                    deliver(Frame(values, null, snapshot), proxy)
                }
            }
            .addOnFailureListener { proxy.close() }
    }

    private fun deliver(frame: Frame, proxy: ImageProxy) {
        try {
            onFrame(frame)
        } finally {
            proxy.close()
        }
    }

    fun close() {
        barcodes.close()
        texts.close()
    }

    private fun upright(proxy: ImageProxy): Bitmap {
        val bitmap = proxy.toBitmap()
        val degrees = proxy.imageInfo.rotationDegrees
        if (degrees == 0) return bitmap
        val m = Matrix().apply { postRotate(degrees.toFloat()) }
        return Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, m, true)
    }
}

@Composable
fun CameraView(modifier: Modifier, torch: Boolean, paused: Boolean, wantText: () -> Boolean, onFrame: (Frame) -> Unit) {
    val context = LocalContext.current
    val owner = LocalLifecycleOwner.current
    val isPaused by rememberUpdatedState(paused)
    val wants by rememberUpdatedState(wantText)
    val handler by rememberUpdatedState(onFrame)
    var camera by remember { mutableStateOf<Camera?>(null) }
    val preview = remember { PreviewView(context).apply { scaleType = PreviewView.ScaleType.FILL_CENTER } }
    val executor = remember { Executors.newSingleThreadExecutor() }
    val analyzer = remember { LabelAnalyzer({ isPaused }, { wants() }, { handler(it) }) }

    DisposableEffect(owner) {
        val future = ProcessCameraProvider.getInstance(context)
        future.addListener({
            val provider = future.get()
            val p = Preview.Builder().build().also { it.setSurfaceProvider(preview.surfaceProvider) }
            @Suppress("DEPRECATION")
            val analysis = ImageAnalysis.Builder()
                .setTargetResolution(Size(1280, 960))
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .build().also { it.setAnalyzer(executor, analyzer) }
            try {
                provider.unbindAll()
                camera = provider.bindToLifecycle(owner, CameraSelector.DEFAULT_BACK_CAMERA, p, analysis)
            } catch (e: Exception) { camera = null }
        }, ContextCompat.getMainExecutor(context))
        onDispose {
            runCatching { future.get().unbindAll() }
            analyzer.close()
            executor.shutdown()
        }
    }
    LaunchedEffect(camera, torch) { camera?.cameraControl?.enableTorch(torch) }
    AndroidView({ preview }, modifier)
}

/** Green flash's sound and buzz, or red's double buzz. */
class Signals(context: Context) {
    private val tones = runCatching { ToneGenerator(AudioManager.STREAM_NOTIFICATION, 90) }.getOrNull()
    private val vibrator: Vibrator? = if (Build.VERSION.SDK_INT >= 31) {
        (context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager)?.defaultVibrator
    } else {
        @Suppress("DEPRECATION")
        context.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
    }

    fun good(sound: Boolean) {
        if (sound) tones?.startTone(ToneGenerator.TONE_PROP_BEEP, 150)
        vibrator?.vibrate(VibrationEffect.createOneShot(80, VibrationEffect.DEFAULT_AMPLITUDE))
    }

    fun bad(sound: Boolean) {
        if (sound) tones?.startTone(ToneGenerator.TONE_PROP_NACK, 300)
        vibrator?.vibrate(VibrationEffect.createWaveform(longArrayOf(0, 160, 120, 160), -1))
    }

    fun release() { tones?.release() }
}

/** The label frame as the pick's proof photo: at most 1280 px, under 1 MB. */
object Photos {
    fun save(context: Context, bitmap: Bitmap?): File? {
        bitmap ?: return null
        return try {
            val scale = minOf(1f, 1280f / maxOf(bitmap.width, bitmap.height))
            val small = if (scale < 1f) Bitmap.createScaledBitmap(bitmap, (bitmap.width * scale).toInt(),
                (bitmap.height * scale).toInt(), true) else bitmap
            var quality = 82
            var bytes: ByteArray
            do {
                val out = ByteArrayOutputStream()
                small.compress(Bitmap.CompressFormat.JPEG, quality, out)
                bytes = out.toByteArray()
                quality -= 10
            } while (bytes.size > 900_000 && quality > 30)
            val dir = File(context.filesDir, "photos").apply { mkdirs() }
            File(dir, UUID.randomUUID().toString() + ".jpg").apply { writeBytes(bytes) }
        } catch (e: Exception) { null }
    }
}
