package com.pixelwa.companion

import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.pixelwa.companion.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: Prefs

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        prefs = Prefs(this)

        binding.serverUrl.setText(prefs.serverBaseUrl)
        binding.apiToken.setText(prefs.apiToken)
        binding.masterSwitch.isChecked = prefs.automationEnabled

        binding.saveBtn.setOnClickListener {
            prefs.serverBaseUrl = binding.serverUrl.text?.toString().orEmpty()
            prefs.apiToken = binding.apiToken.text?.toString().orEmpty()
            prefs.automationEnabled = binding.masterSwitch.isChecked
            applyService()
            Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show()
            binding.statusText.text =
                "Status: ${if (prefs.automationEnabled) "ON" else "OFF"} @ ${prefs.serverBaseUrl}"
        }

        binding.masterSwitch.setOnCheckedChangeListener { _, checked ->
            prefs.automationEnabled = checked
            applyService()
        }

        binding.openAccessibility.setOnClickListener {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }
        binding.openNotifications.setOnClickListener {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        }

        binding.statusText.text =
            "Status: ${if (prefs.automationEnabled) "ON" else "OFF"} @ ${prefs.serverBaseUrl}"
        applyService()
    }

    private fun applyService() {
        val intent = Intent(this, PollingForegroundService::class.java)
        if (prefs.automationEnabled) {
            ContextCompat.startForegroundService(this, intent)
        } else {
            stopService(intent)
        }
    }
}
