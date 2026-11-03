package com.arfa.scan

import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.EditText
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.arfa.scan.databinding.ActivityLoginBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.IOException

/** Sign in with the ARFA email and password (the owner's or a branch's),
 * and the authenticator code when the account has two-step sign-in. */
class LoginActivity : AppCompatActivity() {

    private lateinit var b: ActivityLoginBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityLoginBinding.inflate(layoutInflater)
        setContentView(b.root)
        b.email.setText(Api.session.email)
        showServer()
        b.signIn.setOnClickListener { signIn() }
        b.password.setOnEditorActionListener { _, action, _ ->
            if (action == EditorInfo.IME_ACTION_DONE) { signIn(); true } else false
        }
        b.server.setOnClickListener { changeServer() }
    }

    private fun showServer() {
        b.server.text = getString(R.string.server) + ": " + Api.session.server.removePrefix("https://")
    }

    private fun device(): String = "${Build.MANUFACTURER} ${Build.MODEL}".trim()

    private fun busy(on: Boolean) {
        b.progress.visibility = if (on) View.VISIBLE else View.GONE
        b.signIn.isEnabled = !on
    }

    private fun fail(message: String) {
        b.error.text = message
        b.error.visibility = View.VISIBLE
    }

    private fun signIn() {
        val email = b.email.text?.toString()?.trim().orEmpty()
        val password = b.password.text?.toString().orEmpty()
        if (email.isEmpty() || password.isEmpty()) return fail("Enter your email and password.")
        b.error.visibility = View.GONE
        busy(true)
        lifecycleScope.launch {
            try {
                val body = withContext(Dispatchers.IO) {
                    Api.session.token = null
                    Api.post("/auth/login", JSONObject().put("email", email).put("password", password).put("device", device()))
                }
                Api.session.email = email
                if (body.optBoolean("two_factor")) askCode(body.getString("pending")) else done(body)
            } catch (e: ApiError) {
                fail(e.message ?: "Couldn't sign in.")
            } catch (e: IOException) {
                fail("Can't reach ARFA - check the internet connection.")
            } finally {
                busy(false)
            }
        }
    }

    private fun askCode(pending: String) {
        val input = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            hint = getString(R.string.code)
            setPadding(48, 32, 48, 32)
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.two_step_title)
            .setMessage(R.string.two_step_body)
            .setView(input)
            .setPositiveButton(R.string.go_on) { _, _ ->
                busy(true)
                lifecycleScope.launch {
                    try {
                        val body = withContext(Dispatchers.IO) {
                            Api.post("/auth/two-factor", JSONObject().put("pending", pending)
                                .put("code", input.text.toString().trim()).put("device", device()))
                        }
                        done(body)
                    } catch (e: ApiError) {
                        fail(e.message ?: "That code isn't right.")
                    } catch (e: IOException) {
                        fail("Can't reach ARFA - check the internet connection.")
                    } finally {
                        busy(false)
                    }
                }
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    private fun done(body: JSONObject) {
        Api.session.token = body.getString("token")
        Api.session.me = body
        startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP))
        finish()
    }

    private fun changeServer() {
        val input = EditText(this).apply {
            setText(Api.session.server)
            inputType = InputType.TYPE_TEXT_VARIATION_URI
            setPadding(48, 32, 48, 32)
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.server)
            .setView(input)
            .setPositiveButton(R.string.go_on) { _, _ ->
                val url = input.text.toString().trim()
                if (url.startsWith("https://")) {
                    Api.session.server = url
                    showServer()
                } else {
                    fail("The server address starts with https://")
                }
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }
}
