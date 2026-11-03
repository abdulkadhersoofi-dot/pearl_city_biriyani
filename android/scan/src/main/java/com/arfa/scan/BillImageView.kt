package com.arfa.scan

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.PointF
import android.graphics.RectF
import android.util.AttributeSet
import androidx.core.content.ContextCompat
import com.davemorrissey.labs.subscaleview.SubsamplingScaleImageView
import org.json.JSONArray
import kotlin.math.max
import kotlin.math.min

/** The bill's page, pinch-to-zoom, that glides to a value when it is
 * pointed at and marks it: boxes are [page, ymin, xmin, ymax, xmax] in
 * 0-1000 of the page, as the server's reader gives them. */
class BillImageView @JvmOverloads constructor(context: Context, attrs: AttributeSet? = null) :
    SubsamplingScaleImageView(context, attrs) {

    private var mark: RectF? = null
    private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 3 * resources.displayMetrics.density
        color = ContextCompat.getColor(context, R.color.mark)
    }
    private val fill = Paint().apply { color = 0x332ECC71 }

    init {
        setMaxScale(8f)
        setDoubleTapZoomScale(2.5f)
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val m = mark ?: return
        if (!isReady) return
        val a = sourceToViewCoord(m.left, m.top) ?: return
        val z = sourceToViewCoord(m.right, m.bottom) ?: return
        val r = RectF(a.x - 4, a.y - 4, z.x + 4, z.y + 4)
        canvas.drawRoundRect(r, 6f, 6f, fill)
        canvas.drawRoundRect(r, 6f, 6f, line)
    }

    /** Zooms to the box and marks it (false if it can't yet). */
    fun pointAt(box: JSONArray): Boolean {
        if (!isReady || box.length() < 5) return false
        val w = sWidth.toFloat()
        val h = sHeight.toFloat()
        val y0 = (box.optDouble(1) / 1000 * h).toFloat()
        val x0 = (box.optDouble(2) / 1000 * w).toFloat()
        val y1 = (box.optDouble(3) / 1000 * h).toFloat()
        val x1 = (box.optDouble(4) / 1000 * w).toFloat()
        mark = RectF(x0, y0, x1, y1)
        val padX = max((x1 - x0) * 0.3f, w * 0.05f)
        val padY = max((y1 - y0) * 1.2f, h * 0.035f)
        val scale = min(width / ((x1 - x0) + 2 * padX), height / ((y1 - y0) + 2 * padY))
            .coerceIn(minScale, maxScale)
        animateScaleAndCenter(scale, PointF((x0 + x1) / 2, (y0 + y1) / 2))
            ?.withDuration(320)
            ?.withEasing(SubsamplingScaleImageView.EASE_OUT_QUAD)
            ?.withInterruptible(true)
            ?.start()
        invalidate()
        return true
    }

    fun overview() {
        mark = null
        if (isReady) resetScaleAndCenter()
        invalidate()
    }
}
