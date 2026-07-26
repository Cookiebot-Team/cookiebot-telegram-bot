Feature: Media storage
  As an operator
  I want media stored behind one interface across GCP and S3
  So that switching cloud provider is configuration, not a rewrite

  Background:
    Given a blob store backed by "memory"

  Scenario: Storing and reading back an image
    When a photo of 2048 bytes is stored
    Then the object can be read back byte for byte
    And the key is derived from the content hash

  Scenario: The same image stored twice occupies one object
    When a photo with content "identical bytes" is stored
    And a photo with content "identical bytes" is stored again
    Then both uploads resolve to the same key

  Scenario: Different images get different keys
    When a photo with content "first" is stored
    And a photo with content "second" is stored again
    Then the two uploads resolve to different keys

  Scenario: Media kind decides the file extension
    When a sticker with content "sticker bytes" is stored
    Then the key ends with ".webp"

  Scenario: Reading a key that was never written
    When an object that was never stored is requested
    Then the store reports it as not found

  Scenario: Deleting is idempotent
    When a photo with content "delete me" is stored
    And the object is deleted twice
    Then the object no longer exists

  Scenario: A backend that cannot sign says so
    When a photo with content "sign me" is stored
    And a signed URL is requested
    Then the store reports that it cannot sign URLs

  Scenario: Switching backend to the local filesystem
    Given a blob store backed by "file"
    When a photo with content "on disk" is stored
    Then the object can be read back byte for byte
