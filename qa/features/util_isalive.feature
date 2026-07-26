# Synced from Cookiebot-QA/features/util_isalive.feature.
# The v1 spec repo has 61 scenarios and zero executable steps; v2 wires them to a
# mock Telegram API so every MVP has a real acceptance gate in CI.
Feature: Is Alive
  As a group member
  I want to check whether Cookiebot is responding
  So that I know if the bot is operational

  Background:
    Given the bot is set up in the group

  Scenario: Bot is alive
    Given the bot is running and responsive
    When the user sends "/isalive"
    Then the bot replies confirming it is alive and operational

  Scenario: Bot is down
    Given the bot is not running
    When the user sends "/isalive"
    Then the user receives no response

  Scenario: Alias in Portuguese still works
    Given the bot is running and responsive
    When the user sends "/tavivo"
    Then the bot replies confirming it is alive and operational

  Scenario: Command addressed at a different bot is ignored
    Given the bot is running and responsive
    When the user sends "/isalive@SomeOtherBot"
    Then the user receives no response
