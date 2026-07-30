"""generative_agents.model.llm_model"""

import time
import re
import logging
import requests
from magentic import prompt

from .text_normalize import normalize_llm_output

timing_logger = logging.getLogger("llm_timing")


class LLMModel:
    def __init__(self, config):
        self._api_key = config["api_key"]
        self._base_url = config["base_url"]
        self._model = config["model"]
        self._summary = {"total": [0, 0, 0]}

        self._handle = self.setup(config)
        self._enabled = True

    def setup(self, config):
        raise NotImplementedError(
            "setup is not support for " + str(self.__class__)
        )

    def completion(
        self,
        prompt,
        retry=10,
        callback=None,
        failsafe=None,
        return_type=None,
        caller="llm_normal",
        **kwargs
    ):
        response = None
        self._summary.setdefault(caller, [0, 0, 0])
        call_start = time.perf_counter()
        attempts = 0
        for _ in range(retry):
            attempts += 1
            try:
                output = self._completion(prompt, return_type, **kwargs)
                output = normalize_llm_output(output, return_type)
                self._summary["total"][0] += 1
                self._summary[caller][0] += 1
                if callback:
                    response = callback(output)
                else:
                    response = output
            except Exception as e:
                print(f"LLMModel.completion() caused an error: {e}")
                time.sleep(5)
                response = None
                continue
            if response is not None:
                break
        pos = 2 if response is None else 1
        self._summary["total"][pos] += 1
        self._summary[caller][pos] += 1
        timing_logger.info(
            "llm_call caller=%s model=%s attempts=%d ok=%s duration=%.2fs",
            caller, self._model, attempts, response is not None,
            time.perf_counter() - call_start,
        )
        return response or failsafe

    def _completion(self, prompt, return_type, **kwargs):
        raise NotImplementedError(
            "_completion is not support for " + str(self.__class__)
        )

    def is_available(self):
        return self._enabled  # and self._summary["total"][2] <= 10

    def get_summary(self):
        des = {}
        for k, v in self._summary.items():
            des[k] = "S:{},F:{}/R:{}".format(v[1], v[2], v[0])
        return {"model": self._model, "summary": des}

    def disable(self):
        self._enabled = False


class OpenAILLMModel(LLMModel):
    def setup(self, config):
        from magentic import OpenaiChatModel

        handle = OpenaiChatModel(self._model, api_key=self._api_key, base_url=self._base_url)
        # openai client 預設 timeout=600s：DeepSeek hang 起上嚟單一 call 可以鎖死成個
        # thread 10 分鐘（並行 think 下成步等齊先完）。改短 timeout 交返畀我哋自己嘅
        # retry loop 處理；max_retries=0 避免同外層 retry 相乘。
        timeout = config.get("timeout", 120)
        handle._client = handle._client.with_options(timeout=timeout, max_retries=0)
        handle._async_client = handle._async_client.with_options(timeout=timeout, max_retries=0)
        return handle

    def _completion(self, _prompt, return_type, temperature=0.5):
        @prompt(
            "{_prompt}",
            model=self._handle
        )
        def response(_prompt: str) -> return_type: ...
        output = response(_prompt).res
        return output


class OllamaLLMModel(LLMModel):
    def setup(self, config):
        return None

    def ollama_chat(self, messages, temperature, response_format=None):
        headers = {
            "Content-Type": "application/json"
        }
        params = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if response_format:
            params["response_format"] = response_format

        response = requests.post(
            url=f"{self._base_url}/chat/completions",
            headers=headers,
            json=params,
            stream=False,
            timeout=300
        )
        return response.json()

    def _completion(self, prompt, return_type, temperature=0.5):
        import json
        
        # Generate JSON schema from the Pydantic model for structured output
        response_format = None
        if return_type is not None:
            try:
                schema = return_type.model_json_schema()
                response_format = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": return_type.__name__,
                        "strict": True,
                        "schema": schema
                    }
                }
            except Exception:
                pass
        
        messages = [{"role": "user", "content": prompt}]
        response = self.ollama_chat(messages=messages, temperature=temperature, response_format=response_format)
        
        if response and len(response.get("choices", [])) > 0:
            ret = response["choices"][0]["message"]["content"]
            # 从输出结果中过滤掉<think>标签内的文字，以免影响后续逻辑
            ret = re.sub(r"<think>.*</think>", "", ret, flags=re.DOTALL)
            
            # Parse and validate the response using the Pydantic model
            if return_type is not None:
                try:
                    # Try to parse as JSON and validate with Pydantic
                    parsed = json.loads(ret)
                    validated = return_type.model_validate(parsed)
                    return validated.res
                except json.JSONDecodeError:
                    # If JSON parsing fails, try to extract JSON from the text
                    json_match = re.search(r'\{.*\}', ret, re.DOTALL)
                    if json_match:
                        try:
                            parsed = json.loads(json_match.group())
                            validated = return_type.model_validate(parsed)
                            return validated.res
                        except (json.JSONDecodeError, Exception):
                            pass
                    # If all parsing fails, return the raw text
                    return ret
                except Exception as e:
                    print(f"OllamaLLMModel: Failed to validate response: {e}")
                    return ret
            return ret
        return ""


def create_llm_model(llm_config):
    """Create llm model"""

    if llm_config["provider"] == "ollama":
        return OllamaLLMModel(llm_config)

    elif llm_config["provider"] == "openai":
        return OpenAILLMModel(llm_config)
    else:
        raise NotImplementedError(
            "llm provider {} is not supported".format(llm_config["provider"])
        )
    return None
