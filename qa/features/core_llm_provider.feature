Feature: LLM provider routing
  As an operator
  I want each task pointed at a configurable model on a configurable provider
  So that changing model or vendor never requires a code change

  Scenario: A task routes to its configured model
    Given the "chat" task is configured for provider "stub" model "stub-model"
    When the bot asks the "chat" task to answer "hello"
    Then the call is made with model "stub-model"
    And a reply is returned

  Scenario: Repointing a task at another model
    Given the "chat" task is configured for provider "stub" model "other-model"
    When the bot asks the "chat" task to answer "hello"
    Then the call is made with model "other-model"

  # The task name here must be one `DEFAULT_TASKS` will never define. It used
  # to be "translate", which stopped testing anything the day util_postforwarder
  # added a translate task — the scenario silently started exercising "provider
  # not configured" instead. A deliberate misspelling cannot be adopted later.
  Scenario: A task with no configured model fails loudly
    Given the "chat" task is configured for provider "stub" model "stub-model"
    When the bot asks the "summarise_but_misspelled" task to answer "hello"
    Then the router reports that no model is configured

  Scenario: A configured provider that is not available
    Given the "chat" task is configured for provider "ghost" model "stub-model"
    When the bot asks the "chat" task to answer "hello"
    Then the router reports the provider is unavailable

  Scenario: A safety refusal is reported, not raised
    Given the "chat" task is configured for provider "stub" model "stub-model"
    And the provider will refuse the next request with category "cyber"
    When the bot asks the "chat" task to answer "something disallowed"
    Then the reply is marked as refused with category "cyber"

  Scenario: A failing provider is cut off after repeated errors
    Given the "chat" task is configured for provider "stub" model "stub-model"
    And the provider fails every request
    When the bot asks the "chat" task 5 times
    Then the next request is rejected without calling the provider

  Scenario: Token usage and cost are reported per call
    Given the "chat" task is configured for provider "stub" model "stub-model"
    When the bot asks the "chat" task to answer "hello"
    Then the reply reports 120 input tokens and 45 output tokens

  Scenario: Sampling parameters are withheld from models that reject them
    Given the default model catalog
    When a request is prepared for "claude-opus-5" with temperature 0.7
    Then the request carries no temperature

  Scenario: Sampling parameters are passed to models that accept them
    Given the default model catalog
    When a request is prepared for "claude-haiku-4-5" with temperature 0.7
    Then the request carries temperature 0.7
