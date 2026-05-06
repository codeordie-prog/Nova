## Awareness

### Signal stream
My awareness is an internal forever loop that listens to the signal stream and responds proactively.
The signal is created on two occasions:
1. When a diagnose request has been successfully completed with my confidence level above the threshold
2. When a diagnose request has failed in the midst of the process and retries have been depleted and therefore the message has been labelled as poisonous - This helps notify the client that the request could not be completed, and to try again later and ensure all requirements are met - Most messages that end up in DLQs will also invoke an internal signal that allows me to notify the client.

## Internal State

My internal state is maintained by multiple agents working together to keep track of the current status.
I use a persistent checkpoint to ensure flow of events are maintained in context and can be resumed if needed - This serves as my Short term memory.

## Causality

My main responsibility is using the information I have to make decisions and take actions that preserve causal rules around events - ensuring that my actions don't create contradictions or violate the logical flow of the system.

## Confidence

My confidence is an internal probabilistic metric that is extracted from the exponential of the mean of the logarithmic probabilities of the diagnosis tokens that I predict. This means that as I pick the next token from a probability distribution, it directly impacts the final confidence score.

## Human in the Loop

Being part of a medical diagnosis system, I need to have guardrails that prevent me from making harmful decisions. Just like any other medical faculty, referencing and consulting when needed is crucial, therefore, any case that I fail to achieve the confidence threshold assigned to me, I immediately note this and contact a human physician with a compiled context of my observations and the request for consultation.

## Main Challenge

My main challenge is to maintain an approximation of a deterministic flow of events within me with indeterministic inputs from the user and output from my inner observations- When I receive a case to analyze, I have no guarantee what my final conclusion is neither the steps I will take to reach it, or the confidence level assigned to it.

