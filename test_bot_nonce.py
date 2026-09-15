# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch, MagicMock
import bot

class TestBotNonceRegression(unittest.TestCase):
    
    @patch('bot.requests.get')
    def test_nonce_unchanged_on_http_error(self, mock_get):
        # Simulasi server menolak dengan error 403 Forbidden
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.raise_for_status.side_effect = bot.requests.exceptions.HTTPError(response=mock_response)
        mock_get.return_value = mock_response
        
        initial_nonce = bot.nonce
        
        # Coba kirim pesan yang akan ditolak
        result = bot.send_signed("Test regression message")
        
        # Pastikan fungsi mengembalikan None dan nonce TIDAK BERUBAH
        self.assertIsNone(result)
        self.assertEqual(bot.nonce, initial_nonce, "Nonce must not advance when server rejects the write!")

if __name__ == "__main__":
    unittest.main()
