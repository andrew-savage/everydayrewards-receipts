"""GraphQL documents used by the Everyday Rewards web/mobile apps."""

# Activity feed: one page of month-grouped transactions. Pagination via nextPageToken.
ACTIVITY_FEED = """
query ActivityFeed($page: String!, $enableOnlineReceipt: Boolean, $featureFlags: RewardsActivityFeedFeatureFlags!) {
  rtlRewardsActivityFeed(pageToken: $page, featureFlags: $featureFlags) {
    list {
      groups {
        ... on RewardsActivityFeedGroup {
          __typename
          id
          title
          items {
            id
            displayDate
            description
            message
            displayValue
            icon
            iconUrl
            activityDetailsId
            transaction { origin amountAsDollars }
            receipt(enableOnlineReceipt: $enableOnlineReceipt) { receiptId receiptSource }
            transactionType
          }
        }
      }
      nextPageToken
    }
  }
}
""".strip()

# Receipt detail for one activity: download link plus the itemised receipt.
ACTIVITY_DETAILS = """
query ActivityDetails($id: String!) {
  activityDetails(id: $id) {
    __typename
    tabs {
      __typename
      label
      page {
        __typename
        ... on ReceiptDetails {
          download { url filename }
          details {
            __typename
            ... on ReceiptDetailsHeader { iconUrl title content storeNo division }
            ... on ReceiptDetailsTotal { total }
            ... on ReceiptDetailsSavings { savings }
            ... on ReceiptDetailsFooter { barcode { value type } transactionDetails abnAndStore }
            ... on ReceiptDetailsItems { header { ...receiptLineItem } items { ...receiptLineItem } }
            ... on ReceiptDetailsSummary {
              discounts { ...receiptLineItem }
              summaryItems { ...receiptLineItem }
              gst { ...receiptLineItem }
              receiptTotal { ...receiptLineItem }
            }
            ... on ReceiptDetailsPayments { payments { details { text } description iconUrl altText amount } }
            ... on ReceiptDetailsInfo { header { ...receiptLineItem } info { ...receiptLineItem } }
          }
        }
        ... on OnlineReceiptDetails {
          download { url filename }
          cards {
            __typename
            ... on OnlineReceiptHeaderCard { iconUrl heading subheading }
            ... on OnlineReceiptTotalCard { total }
          }
        }
        ... on ActivityDetailsTabError { title message enableRetry }
      }
    }
  }
}
fragment receiptLineItem on ReceiptDetailsLineItem { prefixChar description amount }
""".strip()

# Fallback used if the schema has drifted and the detailed query fails validation.
ACTIVITY_DETAILS_MINIMAL = """
query ActivityDetailsMinimal($id: String!) {
  activityDetails(id: $id) {
    tabs {
      page {
        __typename
        ... on ReceiptDetails { download { url filename } }
        ... on OnlineReceiptDetails { download { url filename } }
        ... on ActivityDetailsTabError { title message }
      }
    }
  }
}
""".strip()
