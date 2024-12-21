import json
import os
import sys
import traceback

from dotenv import load_dotenv

load_dotenv()
import io
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(
    0, os.path.abspath("../..")
)  # Adds the parent directory to the system path
import pytest
import litellm

import pytest
from litellm.llms.triton.embedding.transformation import TritonEmbeddingConfig
import litellm


def test_split_embedding_by_shape_passes():
    try:
        data = [
            {
                "shape": [2, 3],
                "data": [1, 2, 3, 4, 5, 6],
            }
        ]
        split_output_data = TritonEmbeddingConfig.split_embedding_by_shape(
            data[0]["data"], data[0]["shape"]
        )
        assert split_output_data == [[1, 2, 3], [4, 5, 6]]
    except Exception as e:
        pytest.fail(f"An exception occured: {e}")


def test_split_embedding_by_shape_fails_with_shape_value_error():
    data = [
        {
            "shape": [2],
            "data": [1, 2, 3, 4, 5, 6],
        }
    ]
    with pytest.raises(ValueError):
        TritonEmbeddingConfig.split_embedding_by_shape(
            data[0]["data"], data[0]["shape"]
        )


def test_completion_triton_generate_api():
    try:
        mock_response = MagicMock()

        def return_val():
            return {
                "text_output": "I am an AI assistant",
            }

        mock_response.json = return_val
        mock_response.status_code = 200

        with patch(
            "litellm.llms.custom_httpx.http_handler.HTTPHandler.post",
            return_value=mock_response,
        ) as mock_post:
            response = litellm.completion(
                model="triton/llama-3-8b-instruct",
                messages=[{"role": "user", "content": "who are u?"}],
                max_tokens=10,
                timeout=5,
                api_base="http://localhost:8000/generate",
            )

            # Verify the call was made
            mock_post.assert_called_once()

            # Get the arguments passed to the post request
            print("call args", mock_post.call_args)
            call_kwargs = mock_post.call_args.kwargs  # Access kwargs directly

            # Verify URL
            assert call_kwargs["url"] == "http://localhost:8000/generate"

            # Parse the request data from the JSON string
            request_data = json.loads(call_kwargs["data"])

            # Verify request data
            assert request_data["text_input"] == "who are u?"
            assert request_data["parameters"]["max_tokens"] == 10

            # Verify response
            assert response.choices[0].message.content == "I am an AI assistant"

    except Exception as e:
        print("exception", e)
        import traceback

        traceback.print_exc()
        pytest.fail(f"Error occurred: {e}")
