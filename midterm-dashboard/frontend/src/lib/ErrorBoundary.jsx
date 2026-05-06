import React from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    if (typeof console !== 'undefined') {
      console.error('UI error:', error, info)
    }
  }

  reset = () => this.setState({ error: null })

  render() {
    if (!this.state.error) return this.props.children

    return (
      <div role="alert" aria-live="assertive"
        className="min-h-[60vh] flex items-center justify-center px-6">
        <div className="max-w-md w-full bg-white shadow-sm border border-stone-200 rounded-xl p-6 text-center">
          <AlertTriangle className="h-8 w-8 text-amber-500 mx-auto mb-3" aria-hidden="true" />
          <h2 className="text-lg font-semibold text-stone-800 mb-1">Something went wrong</h2>
          <p className="text-sm text-stone-500 mb-4">
            {this.state.error?.message || 'An unexpected error occurred while rendering this view.'}
          </p>
          <div className="flex gap-2 justify-center">
            <button onClick={this.reset}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm bg-stone-800 text-white hover:bg-stone-700 transition-colors">
              <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" /> Try again
            </button>
            <button onClick={() => window.location.assign('/')}
              className="px-3 py-1.5 rounded-md text-sm bg-stone-100 text-stone-700 hover:bg-stone-200 transition-colors">
              Go home
            </button>
          </div>
        </div>
      </div>
    )
  }
}
