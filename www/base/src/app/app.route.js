/*
 * decaffeinate suggestions:
 * DS207: Consider shorter variations of null checks
 * Full docs: https://github.com/decaffeinate/decaffeinate/blob/master/docs/suggestions.md
 */
class Route {
    constructor($urlRouterProvider, glMenuServiceProvider, config) {
        let apptitle;
        $urlRouterProvider.otherwise('/');
        apptitle = "Flathub builds";
        glMenuServiceProvider.setAppTitle(apptitle);
    }
}
        // all states config are in the modules


angular.module('app')
.config(['$urlRouterProvider', 'glMenuServiceProvider', 'config', Route])
.config(['$locationProvider', function($locationProvider) {
    $locationProvider.hashPrefix('');
}]);
